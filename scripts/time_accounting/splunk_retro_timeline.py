#!/usr/bin/env python3
"""Retroactive time-accounting analysis for ANY Claude Code session (and its
subagents) using data exported from Splunk's index=claude, rather than local
JSONL files -- so this works for sessions whose local transcripts are gone,
from any machine that ever shipped to this Splunk instance.

Reuses the exact same classification logic as session_timeline.py (imported
directly) rather than re-deriving interval/gap math in SPL, which turned out
to be fragile (three separate silent-failure bugs hit trying to do the
cross-source subagent-overlap join purely in SPL: a missing join key field,
a field-name collision silently corrupting the group-by, and consequent row
loss). Splunk is used here only as a raw-event data source; all the actual
time-accounting math is the one already-validated Python implementation.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import session_timeline as st  # noqa: E402

# Path to a vct-splunk-cli checkout, used only to run `splunk search run --export`.
# No default: this repo is public and must not embed any local/private path layout.
VCT_SPLUNK_DIR = os.environ.get("VCT_SPLUNK_CLI_DIR")
if not VCT_SPLUNK_DIR:
    raise RuntimeError("Set VCT_SPLUNK_CLI_DIR to your local vct-splunk-cli checkout path")


def splunk_export(query, earliest="-7d", latest="now"):
    """Run an SPL query via vct-splunk-cli in export mode, return parsed rows."""
    env = dict(os.environ)
    cmd = [
        "doppler", "run", "-p", "iac-conf-mgmt", "-c", "prd", "--",
        "env", "-u", "SPLUNK_TOKEN",
        "SPLUNK_URL=https://splunk-mgmt.pve.jacobpevans.com",
        "SPLUNK_USERNAME=admin",
        "uv", "run", "splunk", "search", "run",
        "--query", query, "--earliest", earliest, "--latest", latest,
        "--export", "--output", "json",
    ]
    result = subprocess.run(cmd, cwd=VCT_SPLUNK_DIR, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"splunk search failed: {result.stderr[-2000:]}")
    data = json.loads(result.stdout)
    return data["data"]["results"]


def fetch_session_events(session_id, earliest="-7d"):
    """Pull every raw event for a sessionId, grouped by source (source path IS
    the per-file grouping key, identical to how local JSONL files are grouped:
    one main file + one file per subagent)."""
    query = (
        f'search index=claude sourcetype=claude:code sessionId="{session_id}" '
        f'| table _raw, source, _time | sort 0 _time'
    )
    rows = splunk_export(query, earliest=earliest)
    by_source = {}
    for r in rows:
        raw = r.get("_raw")
        source = r.get("source", "unknown")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        by_source.setdefault(source, []).append(parsed)
    for source in by_source:
        by_source[source].sort(key=lambda d: d.get("timestamp", ""))
    return by_source


def analyze_session(session_id, earliest="-7d", extend_to_now=False, out_prefix=None):
    by_source = fetch_session_events(session_id, earliest=earliest)
    if not by_source:
        raise RuntimeError(f"No events found in Splunk for sessionId={session_id} (earliest={earliest})")

    main_source = next((s for s in by_source if "/subagents/" not in s), None)
    subagent_sources = [s for s in by_source if "/subagents/" in s]
    if main_source is None:
        main_source = sorted(by_source, key=lambda s: len(by_source[s]))[-1]
        subagent_sources = [s for s in by_source if s != main_source]

    main_data = st.analyze_lines(by_source[main_source], "main", source=main_source)
    if main_data is None:
        raise RuntimeError("Main source had no conversational events")

    subagent_reports = []
    subagent_windows = []
    for source in subagent_sources:
        label = Path(source).stem.replace("agent-", "")
        data = st.analyze_lines(by_source[source], label, source=source)
        if data is None:
            continue
        events = data["events"]
        seg, hook_ms = st.build_timeline(events, data["hook_ms"])
        totals, top = st.summarize(seg)
        window = (events[0]["ts"], events[-1]["ts"], label)
        subagent_windows.append(window)
        subagent_reports.append({"label": label, "segments": seg, "totals": totals, "top": top,
                                 "hook_ms": hook_ms, "window": window})

    extend_to = st.UTC_NOW if extend_to_now else None
    main_segments, main_hook_ms = st.build_timeline(main_data["events"], main_data["hook_ms"],
                                                     subagent_windows, extend_to=extend_to)
    main_totals, main_top = st.summarize(main_segments)

    report = {
        "session_id": session_id,
        "source": "splunk-retroactive",
        "main": {
            "totals": {k: round(v, 1) for k, v in main_totals.items()},
            "top": [[k, round(v, 1)] for k, v in main_top],
            "hook_overhead_ms": main_hook_ms,
            "segments": [[s.isoformat(), e.isoformat(), c, l] for s, e, c, l in main_segments],
        },
        "subagents": [
            {
                "label": r["label"],
                "totals": {k: round(v, 1) for k, v in r["totals"].items()},
                "top": [[k, round(v, 1)] for k, v in r["top"]],
                "window": [r["window"][0].isoformat(), r["window"][1].isoformat()],
                "segments": [[s.isoformat(), e.isoformat(), c, l] for s, e, c, l in r["segments"]],
            }
            for r in subagent_reports
        ],
    }

    if out_prefix:
        Path(out_prefix).with_suffix(".json").write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: splunk_retro_timeline.py <sessionId> [earliest] [out_prefix] [--extend-to-now]", file=sys.stderr)
        sys.exit(1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    extend = "--extend-to-now" in sys.argv
    sid = args[0]
    earliest_arg = args[1] if len(args) > 1 else "-7d"
    out = args[2] if len(args) > 2 else f"splunk_retro_{sid[:8]}"
    rep = analyze_session(sid, earliest=earliest_arg, extend_to_now=extend, out_prefix=out)
    # rep['main']['totals'] values are SECONDS (session_timeline.py's convention); convert here for display.
    print(f"Main totals (min): { {k: round(v/60, 1) for k, v in rep['main']['totals'].items()} }")
    print(f"Subagents analyzed: {[s['label'] for s in rep['subagents']]}")
