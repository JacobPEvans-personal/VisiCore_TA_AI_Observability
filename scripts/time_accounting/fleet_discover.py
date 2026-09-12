#!/usr/bin/env python3
"""Fleet-wide retroactive time accounting: discover every distinct sessionId in
index=claude over a time window (no a priori sessionId needed), analyze each one
(main + all its subagents, via splunk_retro_timeline.analyze_session), then
aggregate into one cross-session view of what is actually blocking AI agents --
"where does time go, fleet-wide" rather than one session at a time.

sessionId is the correct fleet-grouping key: subagent transcripts share the
parent's sessionId (see VCT_AI_Observability README / claude:code:subagent
finding), so `stats ... by sessionId` already naturally rolls parent + children
together at discovery time.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import splunk_retro_timeline as srt  # noqa: E402
import session_timeline as st  # noqa: E402


def discover_sessions(earliest="-24h"):
    """Return [{sessionId, events, first, last}] for every session with activity
    in the window, most-events-first."""
    query = (
        'search index=claude sourcetype=claude:code '
        '| stats count as events, min(_time) as first, max(_time) as last by sessionId '
        '| sort -events'
    )
    rows = srt.splunk_export(query, earliest=earliest)
    return [
        {"sessionId": r["sessionId"], "events": int(r["events"]),
         "first": float(r["first"]), "last": float(r["last"])}
        for r in rows
    ]


def all_segments(report):
    """Yield (cat, label, duration_sec) for every segment in a session report
    (main + all its subagents) -- the full per-segment data, not the truncated
    top-15 each analyze_session already computed, so fleet aggregation doesn't
    silently drop long-tail labels."""
    for seg in report["main"]["segments"]:
        s, e, cat, label = seg
        yield cat, label, (st.parse_ts(e) - st.parse_ts(s)).total_seconds()
    for sub in report["subagents"]:
        for seg in sub["segments"]:
            s, e, cat, label = seg
            yield cat, label, (st.parse_ts(e) - st.parse_ts(s)).total_seconds()


def analyze_fleet(earliest="-24h", recent_active_min=10.0, out_prefix="fleet_report"):
    sessions = discover_sessions(earliest)
    now = datetime.now(timezone.utc).timestamp()

    per_session = []
    fleet_totals = {c: 0.0 for c in st.CATEGORIES}
    fleet_by_label = {}

    for s in sessions:
        still_active = (now - s["last"]) / 60.0 < recent_active_min
        try:
            report = srt.analyze_session(s["sessionId"], earliest=earliest, extend_to_now=still_active)
        except RuntimeError as exc:
            print(f"  skip {s['sessionId']}: {exc}", file=sys.stderr)
            continue

        totals = {c: 0.0 for c in st.CATEGORIES}
        for cat, label, dur in all_segments(report):
            totals[cat] = totals.get(cat, 0.0) + dur
            fleet_totals[cat] = fleet_totals.get(cat, 0.0) + dur
            fleet_by_label[label] = fleet_by_label.get(label, 0.0) + dur

        per_session.append({
            "sessionId": s["sessionId"],
            "events": s["events"],
            "still_active": still_active,
            "subagent_count": len(report["subagents"]),
            "totals_min": {k: round(v / 60, 1) for k, v in totals.items()},
        })
        print(f"  {s['sessionId'][:8]}: {len(report['subagents'])} subagents, "
              f"TOOL_EXEC={totals['TOOL_EXEC']/60:.1f}m HUMAN_IDLE={totals['HUMAN_IDLE']/60:.1f}m "
              f"active={still_active}")

    fleet_top_labels = sorted(fleet_by_label.items(), key=lambda kv: -kv[1])[:25]
    per_session.sort(key=lambda r: -sum(r["totals_min"].values()))

    fleet_report = {
        "earliest": earliest,
        "sessions_analyzed": len(per_session),
        "fleet_totals_min": {k: round(v / 60, 1) for k, v in fleet_totals.items()},
        "fleet_top_blocking_labels_min": [[label, round(v / 60, 1)] for label, v in fleet_top_labels],
        "sessions_by_total_time": per_session,
    }
    Path(out_prefix).with_suffix(".json").write_text(json.dumps(fleet_report, indent=2))
    return fleet_report


if __name__ == "__main__":
    earliest_arg = sys.argv[1] if len(sys.argv) > 1 else "-24h"
    out = sys.argv[2] if len(sys.argv) > 2 else "fleet_report"
    print(f"Discovering + analyzing all sessions in index=claude, earliest={earliest_arg} ...")
    rep = analyze_fleet(earliest=earliest_arg, out_prefix=out)
    print(f"\nFleet totals (min): {rep['fleet_totals_min']}")
    print(f"Sessions analyzed: {rep['sessions_analyzed']}")
    print("\nTop 10 fleet-wide blocking labels (min):")
    for label, mins in rep["fleet_top_blocking_labels_min"][:10]:
        print(f"  {mins:>7.1f}  {label}")
