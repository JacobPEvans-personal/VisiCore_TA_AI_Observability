#!/usr/bin/env python3
"""Reconstruct a time-accounting breakdown of a Claude Code session from its
local JSONL transcript (main session + subagent/teammate files).

Ground truth verified directly against the live JSONL for this session
(see hashed-tickling-sunset.md plan for the derivation):
  - assistant message content[] may contain tool_use blocks (id, name, input)
  - the matching tool_result arrives as the next primary "user" event, with
    message.content[0] = {type: "tool_result", tool_use_id: <matches id>},
    and top-level parentUuid == the assistant message's uuid (verified: this
    always held, sourceToolAssistantUUID duplicates parentUuid on these lines)
  - "human" turns: message.content is a plain string (or a non-tool_result
    array) -- anything NOT a tool_result is a "content arrived" event that
    ends a waiting gap.
  - subagent/teammate files live at <session>/subagents/agent-*.jsonl, share
    the parent's sessionId, and are NOT correlated back to a specific parent
    tool_use in every case (some are async/queued "teammate" dispatches, not
    a blocking tool_use/tool_result pair) -- so parent-side gaps are
    reclassified to SUBAGENT_WAIT by *time-window overlap* with each
    subagent's own [first_ts, last_ts], not by id-matching alone.
"""
import bisect
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

UTC_NOW = datetime.now(timezone.utc)

# MODEL_ACTIVE is split in two: Claude Code sometimes logs an empty placeholder
# "thinking" block as its own assistant-type transcript line, with the real
# content (text or a tool call) arriving on a SEPARATE line a moment later --
# verified directly against a live session (empty-thinking line at
# 14:58:56.078Z, real 172-char text line at 14:58:57.080Z, one second apart).
# A naive "gap before the next assistant event = thinking" classifier pins the
# whole gap's duration on whichever line comes next, which is sometimes the
# empty placeholder -- so it can't distinguish confirmed generation from time
# whose outcome isn't visible in this transcript. Measured on a live session:
# 44.4% of what a single MODEL_ACTIVE bucket would report as "thinking" time
# lands on a transcript line with zero measurable text/thinking/tool-input
# chars. Report both, never collapse them back into one confident number.
CATEGORIES = ["TOOL_EXEC", "SUBAGENT_WAIT", "MODEL_ACTIVE_CONFIRMED", "MODEL_ACTIVE_UNVERIFIED",
              "HUMAN_IDLE", "SYSTEM_OVERHEAD"]


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z" if "." in s else "%Y-%m-%dT%H:%M:%S%z")


def load_lines(path):
    lines = []
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines


def primary_events(lines):
    """Extract the ordered primary chain: assistant turns (with any tool_use
    blocks) and content-arrival events (human msgs + tool_results), each
    timestamped. Also sums explicit hook durationMs found in attachments."""
    events = []
    hook_ms_total = 0
    for ln in lines:
        t = ln.get("type")
        ts_raw = ln.get("timestamp")
        if t == "attachment":
            dur = (ln.get("attachment") or {}).get("durationMs")
            if isinstance(dur, (int, float)):
                hook_ms_total += dur
            continue
        if t not in ("user", "assistant") or not ts_raw:
            continue
        ts = parse_ts(ts_raw)
        msg = ln.get("message") or {}
        content = msg.get("content")
        if t == "assistant":
            tool_uses = []
            content_chars = 0
            tool_input_chars = 0
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tu_input = block.get("input") or {}
                        tool_uses.append({"id": block.get("id"), "name": block.get("name"), "input": tu_input})
                        tool_input_chars += len(json.dumps(tu_input))
                    elif block.get("type") in ("text", "thinking"):
                        content_chars += len(block.get("text") or block.get("thinking") or "")
            # output_chars is the full "did this line produce anything measurable" signal:
            # text/thinking content plus tool_use input size (a pure tool call with no
            # narration still legitimately produced something -- deciding the tool and
            # generating its input args -- so it must count as confirmed output too).
            events.append({"kind": "assistant", "ts": ts, "tool_uses": tool_uses, "uuid": ln.get("uuid"),
                            "output_chars": content_chars + tool_input_chars})
        else:  # user
            first_block = content[0] if isinstance(content, list) and content else None
            if isinstance(first_block, dict) and first_block.get("type") == "tool_result":
                events.append({"kind": "tool_result", "ts": ts,
                               "tool_use_id": first_block.get("tool_use_id"),
                               "parent_uuid": ln.get("parentUuid")})
            else:
                events.append({"kind": "human", "ts": ts})
    events.sort(key=lambda e: e["ts"])
    return events, hook_ms_total


SUBAGENT_TOOL_NAMES = {"agent", "task"}


def label_tool(tu):
    name = tu.get("name") or "unknown"
    if name == "Bash":
        cmd = (tu.get("input") or {}).get("command", "")
        return f"Bash: {cmd[:80]}"
    if name.startswith("mcp__"):
        return f"MCP: {name}"
    if name == "TaskOutput":
        tid = (tu.get("input") or {}).get("task_id", "?")
        return f"TaskOutput: task_id={tid}"
    path = (tu.get("input") or {}).get("file_path")
    return f"{name}: {path}" if path else name


def build_timeline(events, hook_ms_total, subagent_windows=None, extend_to=None):
    """Walk the primary chain producing (start, end, category, label) segments.

    extend_to: if the transcript's last event predates this timestamp (e.g. wall-clock
    "now" for a still-running/stalled session), append a trailing gap up to it so a
    session that has simply gone quiet doesn't silently vanish from the report.
    """
    subagent_windows = subagent_windows or []
    segments = []
    pending_tool_uses = {}  # tool_use_id -> (assistant_ts, tool_use dict)

    i = 0
    n = len(events)
    last_content_ts = events[0]["ts"] if events else None

    while i < n:
        e = events[i]
        if e["kind"] == "assistant":
            gap_start = last_content_ts
            gap_end = e["ts"]
            if gap_start is not None and gap_end > gap_start:
                if e["output_chars"] > 0:
                    segments.append([gap_start, gap_end, "MODEL_ACTIVE_CONFIRMED",
                                      f"model thinking+generating ({e['output_chars']} chars produced)"])
                else:
                    segments.append([gap_start, gap_end, "MODEL_ACTIVE_UNVERIFIED",
                                      "gap before a transcript line with no measurable output "
                                      "(likely an empty placeholder block; real content may be on the next line)"])
            for tu in e["tool_uses"]:
                if tu["id"]:
                    pending_tool_uses[tu["id"]] = (e["ts"], tu)
            if not e["tool_uses"]:
                last_content_ts = e["ts"]  # plain text turn; next gap is HUMAN_IDLE until reclassified
        elif e["kind"] == "tool_result":
            match = pending_tool_uses.pop(e["tool_use_id"], None)
            if match:
                start_ts, tu = match
                name = (tu.get("name") or "").lower()
                category = "SUBAGENT_WAIT" if name in SUBAGENT_TOOL_NAMES else "TOOL_EXEC"
                segments.append([start_ts, e["ts"], category, label_tool(tu)])
            last_content_ts = e["ts"]
        else:  # human
            last_content_ts = e["ts"]
        i += 1

    # Any tool_use never resolved: this is either genuinely still running (true for the
    # live/local case, where "no result yet" really means in-flight), or -- when the
    # source is a lossy copy (e.g. Splunk-exported data, which can silently drop a small
    # fraction of individual events even though the bulk ingests fine) -- the real
    # tool_result exists but never made it into this dataset. Blindly closing at the
    # session's overall LAST event conflates these and turns one missing event into a
    # wildly inflated bogus duration (a single dropped tool_result for an early call can
    # look like it "ran" for the entire rest of the session). Bound each orphan instead at
    # the next event that actually happened afterward, if any -- something else occurring
    # proves the orphaned call couldn't still be running past that point. Only fall back to
    # the tail/extend_to when the orphan truly is the last thing recorded.
    if pending_tool_uses and events:
        tail = extend_to if extend_to is not None else events[-1]["ts"]
        all_ts_sorted = sorted(e["ts"] for e in events)
        for start_ts, tu in pending_tool_uses.values():
            name = (tu.get("name") or "").lower()
            category = "SUBAGENT_WAIT" if name in SUBAGENT_TOOL_NAMES else "TOOL_EXEC"
            idx = bisect.bisect_right(all_ts_sorted, start_ts)
            close_ts = all_ts_sorted[idx] if idx < len(all_ts_sorted) else tail
            label_suffix = " (still running)" if close_ts == tail else " (orphaned -- likely missing result event)"
            segments.append([start_ts, close_ts, category, label_tool(tu) + label_suffix])

    # Turn the implicit "waiting for a human message" gaps into explicit segments too:
    # any point where the chain goes assistant(no tool_use) -> human, we already recorded
    # nothing; add HUMAN_IDLE for those spans that aren't already covered by a segment.
    covered = sorted(segments, key=lambda s: s[0])
    filled = []
    cursor = events[0]["ts"] if events else None
    for seg in covered:
        if cursor is not None and seg[0] > cursor:
            filled.append([cursor, seg[0], "HUMAN_IDLE", "awaiting next input"])
        filled.append(seg)
        cursor = max(cursor or seg[1], seg[1])
    if events and cursor is not None and cursor < events[-1]["ts"]:
        filled.append([cursor, events[-1]["ts"], "HUMAN_IDLE", "awaiting next input"])
        cursor = events[-1]["ts"]
    if events and extend_to is not None and cursor is not None and cursor < extend_to:
        filled.append([cursor, extend_to, "HUMAN_IDLE", "quiet since last transcript event (as of report run time)"])
    filled.sort(key=lambda s: s[0])

    # Reclassify HUMAN_IDLE overlap with subagent activity windows -> SUBAGENT_WAIT
    result = []
    for start, end, cat, label in filled:
        if cat != "HUMAN_IDLE" or not subagent_windows:
            result.append([start, end, cat, label])
            continue
        overlaps = []
        for w_start, w_end, w_label in subagent_windows:
            os_, oe_ = max(start, w_start), min(end, w_end)
            if os_ < oe_:
                overlaps.append((os_, oe_, w_label))
        if not overlaps:
            result.append([start, end, cat, label])
            continue
        # Merge overlapping/adjacent subagent windows first (concurrent subagents must
        # not each claim their own slice of the same wall-clock time -- that produced
        # negative-duration segments and silently ate real minutes from the total).
        overlaps.sort()
        merged = [list(overlaps[0])]
        for os_, oe_, w_label in overlaps[1:]:
            if os_ <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], oe_)
                merged[-1][2] = merged[-1][2] + f", {w_label}"
            else:
                merged.append([os_, oe_, w_label])
        cur = start
        for os_, oe_, w_label in merged:
            if os_ > cur:
                result.append([cur, os_, "HUMAN_IDLE", label])
            result.append([os_, oe_, "SUBAGENT_WAIT", f"background: {w_label}"])
            cur = oe_
        if cur < end:
            result.append([cur, end, "HUMAN_IDLE", label])

    # TaskOutput (and other backgrounded polling calls) is a black box on its own --
    # "waiting 150 minutes on TaskOutput" doesn't say on WHAT. Attribute each such
    # call to whichever subagent(s) were actually running during its span, via the
    # same window-overlap logic used above for idle gaps -- never leave "waiting on
    # a subagent" unresolved when the subagent's own activity window is known.
    if subagent_windows:
        attributed = []
        for start, end, cat, label in result:
            if not label.startswith("TaskOutput") or not subagent_windows:
                attributed.append([start, end, cat, label])
                continue
            overlaps = [w_label for w_start, w_end, w_label in subagent_windows
                        if max(start, w_start) < min(end, w_end)]
            if overlaps:
                attributed.append([start, end, "SUBAGENT_WAIT", f"{label} (waiting on: {', '.join(overlaps)})"])
            else:
                attributed.append([start, end, cat, label + " (background task, no matching subagent window)"])
        result = attributed

    result.sort(key=lambda s: s[0])
    return result, hook_ms_total


def summarize(segments):
    totals = {c: 0.0 for c in CATEGORIES}
    by_label = {}
    for start, end, cat, label in segments:
        dur = (end - start).total_seconds()
        totals[cat] = totals.get(cat, 0.0) + dur
        by_label[label] = by_label.get(label, 0.0) + dur
    top = sorted(by_label.items(), key=lambda kv: -kv[1])[:15]
    return totals, top


def analyze_lines(lines, label, source="unknown"):
    """Core entry point: works on an already-loaded list of parsed JSON dicts,
    whether read from a local JSONL file or exported from Splunk's _raw field --
    both are the identical schema, so this is the one place classification logic
    lives for either source."""
    events, hook_ms = primary_events(lines)
    if not events:
        return None
    return {"path": source, "label": label, "lines": lines, "events": events, "hook_ms": hook_ms}


def analyze_file(path, label):
    return analyze_lines(load_lines(path), label, source=str(path))


def main():
    main_jsonl = Path(sys.argv[1])
    subagents_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else main_jsonl.parent / main_jsonl.stem / "subagents"
    out_prefix = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("session_report")

    main_data = analyze_file(main_jsonl, "main")
    if main_data is None:
        print(f"No conversational events found in {main_jsonl}", file=sys.stderr)
        sys.exit(1)

    subagent_reports = []
    subagent_windows = []
    if subagents_dir.exists():
        for f in sorted(subagents_dir.glob("agent-*.jsonl")):
            slug = f.stem.replace("agent-", "")
            meta_path = f.with_suffix(".meta.json")
            desc = slug
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    desc = meta.get("description") or meta.get("agentType") or slug
                except Exception:
                    pass
            data = analyze_file(f, desc)
            if not data:
                continue
            events = data["events"]
            # Not extended to "now": a finished subagent has no post-hoc idle time of
            # its own, it simply stopped -- only the still-open main session needs that.
            seg, hook_ms = build_timeline(events, data["hook_ms"])
            totals, top = summarize(seg)
            window = (events[0]["ts"], events[-1]["ts"], desc)
            subagent_windows.append(window)
            subagent_reports.append({"label": desc, "segments": seg, "totals": totals, "top": top,
                                     "hook_ms": hook_ms, "window": window})

    main_segments, main_hook_ms = build_timeline(main_data["events"], main_data["hook_ms"],
                                                  subagent_windows, extend_to=UTC_NOW)
    main_totals, main_top = summarize(main_segments)

    report = {
        "generated_note": "point-in-time snapshot; session may still be running",
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

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    (out_prefix.with_suffix(".json")).write_text(json.dumps(report, indent=2))

    # Markdown
    lines_out = ["# Session time-accounting report\n"]
    lines_out.append(f"Point-in-time snapshot (session may still be running).\n")
    total_sec = sum(main_totals.values())
    lines_out.append("## Main session totals\n")
    lines_out.append("| Category | Duration | % |\n|---|---|---|")
    for c in CATEGORIES:
        v = main_totals.get(c, 0.0)
        pct = (v / total_sec * 100) if total_sec else 0
        lines_out.append(f"| {c} | {v/60:.1f} min | {pct:.1f}% |")
    lines_out.append(f"\nHook overhead observed (informational, not separately charged): {main_hook_ms} ms\n")
    lines_out.append("## Top time sinks (main session)\n")
    lines_out.append("| Label | Duration |\n|---|---|")
    for label, v in main_top:
        lines_out.append(f"| {label} | {v/60:.1f} min |")

    for r in subagent_reports:
        lines_out.append(f"\n## Subagent: {r['label']}\n")
        lines_out.append("_HUMAN_IDLE here means waiting on the orchestrator/parent session to "
                          "send a follow-up or continuation -- a subagent has no direct human to wait on._\n")
        sub_total = sum(r["totals"].values())
        lines_out.append("| Category | Duration | % |\n|---|---|---|")
        for c in CATEGORIES:
            v = r["totals"].get(c, 0.0)
            pct = (v / sub_total * 100) if sub_total else 0
            display = "HUMAN_IDLE (awaiting orchestrator follow-up)" if c == "HUMAN_IDLE" else c
            lines_out.append(f"| {display} | {v/60:.1f} min | {pct:.1f}% |")
        lines_out.append("\n_Top time sinks for this subagent (what it was actually doing/waiting on):_\n")
        lines_out.append("| Label | Duration |\n|---|---|")
        for label, v in r["top"]:
            lines_out.append(f"| {label} | {v/60:.1f} min |")

    (out_prefix.with_suffix(".md")).write_text("\n".join(lines_out) + "\n")
    print(f"Wrote {out_prefix.with_suffix('.json')} and {out_prefix.with_suffix('.md')}")
    print(f"Main totals (min): { {k: round(v/60,1) for k,v in main_totals.items()} }")


if __name__ == "__main__":
    main()
