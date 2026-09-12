# VisiCore TA for AI Observability

Splunk Technology Add-on providing field extractions, OTel semantic convention aliases, macros, and lookups for AI coding tool observability.
Companion to [VisiCore_App_for_AI_Observability](https://github.com/JacobPEvans/VisiCore_App_for_AI_Observability).

## Architecture

```text
Filesystem -> Cribl Edge -> Cribl Stream -> Splunk HEC -> Splunk Enterprise
   (JSON)      (packs)      (routing)       (port 8088)    (index=claude)
```

## Knowledge Objects

- **props.conf** - Field extractions for 30 sourcetypes (Claude, Gemini, Antigravity, VS Code, GitHub Copilot, macOS)
- **transforms.conf** - Wildcard model-pricing lookup definition
- **macros.conf** - 14 reusable search macros (index filters, base filters, dedup, token extraction, lookup-driven cost calc, tools, cache, time-accounting)
- **eventtypes.conf** - 7 event types for Claude, Gemini, and Copilot events
- **tags.conf** - ai/llm/genai tags for CIM compliance
- **lookups/** - `ai_model_pricing.csv` wildcard pricing table

## Token Model

Aligned with [ccusage](https://github.com/ryoppippi/ccusage). Four token types, exposed as canonical convenience fields:

| Token Type | Raw Field | Convenience Field |
|---|---|---|
| Input | `message.usage.input_tokens` | `input_tokens` |
| Output | `message.usage.output_tokens` | `output_tokens` |
| Cache Read | `message.usage.cache_read_input_tokens` | `cache_read_tokens` |
| Cache Creation | `message.usage.cache_creation_input_tokens` | `cache_creation_tokens` |

`total_tokens` = sum of all four. Deduplication via a null-safe `messageId:requestId` composite key
(falls back to `uuid`/`noreq` when either part is missing — `dedup` would otherwise silently drop those events).

## Pricing Lookup

`lookups/ai_model_pricing.csv` is the single source of truth for cost calculation — no macro or dashboard may hardcode prices.

- **Wildcard matching**: `transforms.conf` declares `match_type = WILDCARD(model_pattern)` with `max_matches = 1`,
  so the **first matching row in CSV file order wins**. Keep rows ordered most-specific-first; the `*` catch-all must stay last.
  Only `*` is special — `[`, `]`, `<`, `>` match literally, so `claude-fable-5*` also matches `claude-fable-5[1m]`
  and `<synthetic>` matches exactly.
- **Never drop events**: unknown models match the `*` catch-all at $0 and are flagged `pricing_known=false`,
  which the App surfaces as an "Unpriced Messages" KPI. Add a row (above the catch-all) to price a new model.
- **Cache creation pricing** uses the 5-minute cache-write rate (1.25x input), matching what Claude Code reports
  in `cache_creation_input_tokens`.
- **Updating prices**: edit the CSV only, using the official pricing pages
  ([Anthropic](https://platform.claude.com/docs/en/about-claude/pricing),
  [Google](https://ai.google.dev/gemini-api/docs/pricing),
  [OpenAI](https://platform.openai.com/docs/pricing)).
  Long-context (1M) requests bill at standard per-token rates, so no separate `[1m]` rows are needed.

## Macros

| Macro | Purpose |
|---|---|
| `claude_index`, `gemini_index`, `openai_index`, `copilot_index` | Per-provider index filters (override in `local/` to relocate) |
| `ai_all_indexes` | All provider indexes, composed from the four macros above |
| `claude_assistant_events` | Claude assistant messages (the costed events) |
| `claude_all_session_events` | Session + subagent events |
| `dedup_messages` | Null-safe dedup by `messageId:requestId` |
| `extract_tokens` | Canonical 4-token convenience fields + `total_tokens` |
| `calculate_cost` | Lookup-driven `cost_usd` + `pricing_known` flag (includes `extract_tokens`) |
| `claude_metric_events` | **Canonical dashboard base**: assistant events, deduped, with tokens + cost |
| `extract_tools` | Tool-use extraction with CIM Change fields |
| `calculate_cache_pct` | Per-event cache hit percentage |
| `claude_time_accounting_by_gap(sessionId)` | Full (unfiltered) per-gap time accounting into 6 buckets: `TOOL_EXEC`, `SUBAGENT_WAIT`, `MODEL_ACTIVE_CONFIRMED`, `MODEL_ACTIVE_UNVERIFIED`, `HUMAN_IDLE`, `SYSTEM_OVERHEAD`. Pass a real `sessionId` for one session, or `"*"` for fleet-wide. Classification is per-`source` in isolation — see [Time Accounting Tool](#time-accounting-tool) for cross-source overlap correlation, and for why "model thinking" is split into confirmed vs. unverified. |

## OTel Field Mapping

| Native Field | OTel Convention |
|---|---|
| message.model | gen_ai.response.model |
| sessionId | gen_ai.conversation.id |
| input + cache_read + cache_creation tokens | gen_ai.usage.input_tokens |
| message.usage.output_tokens | gen_ai.usage.output_tokens |
| message.usage.cache_read_input_tokens | gen_ai.usage.cache_read.input_tokens |
| message.usage.cache_creation_input_tokens | gen_ai.usage.cache_creation.input_tokens |
| message.stop_reason | gen_ai.response.finish_reasons |
| "anthropic" | gen_ai.provider.name |

> **Semantics note**: per the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/),
> `gen_ai.usage.input_tokens` SHOULD include cached tokens, so it is computed as input + cache_read + cache_creation.
> The native convenience field `input_tokens` remains *uncached input only* (Anthropic API / ccusage semantics).
> Pick the field family that matches your consumer.

## CIM Data Model Mapping

| Native Field | CIM Field | Data Model |
|---|---|---|
| type | action | Web, Change |
| message.model | dest | Web |
| sessionId | session_id | Web |
| "anthropic" | vendor | All |
| "claude_code" | product | All |
| tool name | object, command | Change |
| input.file_path | object_path | Change |

## Installation

Install this TA before the companion App:

```bash
splunk install app VisiCore_TA_AI_Observability-*.tar.gz
splunk restart
```

Ensure indexes exist: `claude`, `gemini` (plus `vscode`, `openai`, `mac_perf`, `os` if those feeds are enabled).

## Usage

Start dashboard base searches with the composed macro:

```spl
`claude_metric_events` | timechart sum(total_tokens) as tokens, sum(cost_usd) as cost
```

Validation smoke test — every model seen in live data should resolve `pricing_known=true`
except `<synthetic>` and genuinely unknown models:

```spl
`claude_metric_events` | stats sum(cost_usd) as cost, count by model, pricing_known
```

## Time Accounting Tool

`claude_time_accounting_by_gap` classifies gaps per-`source` (one session or
subagent JSONL file) in isolation, so it can't tell that a `HUMAN_IDLE`-looking
gap in the main session actually overlapped with a concurrently-running
subagent in a *different* `source` file — that needs cross-file timestamp
interval correlation, which SPL's `join` handles poorly (silently dropped join
keys, `overwrite=true` clobbering group-by fields, uncontrolled cross-products).

**Why "model thinking" is split into confirmed vs. unverified**: an earlier
version of this macro reported one `MODEL_ACTIVE` bucket for any gap with no
tool_use and no text content, implying every such gap was legitimate
generation time. Verified directly against production (2026-07-24): 100% of
the rows that reach that branch have EMPTY thinking content — Claude Code logs
an empty placeholder `thinking` block as its own transcript row, with the real
content (text or a tool call) landing on a *separate* row moments later. A
single confident `MODEL_ACTIVE` number was therefore overclaiming — this
macro's own gap-attribution model (classify the gap *after* a row, by that
row's own content) structurally routes every row with real content into
`HUMAN_IDLE`/`TOOL_EXEC` before it can ever reach the `MODEL_ACTIVE` branch,
so `MODEL_ACTIVE_CONFIRMED` will read near-zero via this macro specifically —
that's an accurate reflection of what this gap shape can prove, not a bug. The
Python tool below uses a different attribution model (classify the gap
*before* each row, by that row's own output) and found a materially different
split on a live session (~56% confirmed / ~44% unverified) — the two numbers
are not directly comparable, both are correct under their own definitions.

`scripts/time_accounting/` provides a Python complement for exactly this case:

- `session_timeline.py` — the core classifier (stdlib only). Same 6-bucket
  taxonomy as the macro, but pairs `tool_use`/`tool_result` by ID, reads hook
  `durationMs` directly, and reclassifies a parent session's gaps as
  `SUBAGENT_WAIT` whenever they overlap a subagent's observed time window.
- `splunk_retro_timeline.py` — runs the same classifier against events
  exported from Splunk (`index=claude`) instead of local JSONL, so it works for
  any session/subagent set still present in Splunk, from any machine that ever
  shipped to this instance. Requires a
  [vct-splunk-cli](https://github.com/JacobPEvans-personal/vct-splunk-cli)
  checkout; point `VCT_SPLUNK_CLI_DIR` at it (no default — this repo is public
  and must not embed a local path layout):

  ```bash
  VCT_SPLUNK_CLI_DIR=/path/to/vct-splunk-cli \
    python3 scripts/time_accounting/splunk_retro_timeline.py <sessionId> [earliest] [out_prefix] [--extend-to-now]
  ```

Validated against local ground truth on a live session: single-digit-minute
agreement across all 5 categories between the local JSONL path and the
Splunk-export path.

`fleet_discover.py` closes the "any session, no prior knowledge" gap: it
discovers every distinct `sessionId` active in `index=claude` over a time
window (no sessionId needed up front), runs `splunk_retro_timeline` on each,
and aggregates into one fleet-wide view — total time by category across every
session, and the top blocking labels (tools, commands, MCP calls) ranked by
total minutes lost, fleet-wide. This is the direct way to answer "what is
actually blocking AI agents across the whole homelab, not just one session":

```bash
VCT_SPLUNK_CLI_DIR=/path/to/vct-splunk-cli \
  python3 scripts/time_accounting/fleet_discover.py [earliest] [out_prefix]
```

Validated against production (`-24h`, 14 sessions): correctly surfaced a
hung `mcp__zammad__zammad_create_ticket` call (216 min, matching a
single-session finding) and independently found a second, previously-unknown
orphaned tool call in an unrelated session — proving the fleet-wide rollup
finds real, actionable stalls that single-session analysis would miss.

## Packaging

```bash
./scripts/package.sh
```

Produces a versioned tarball in `build/`.

## Roadmap

- v0.3: OTel-metrics cost source variant using `claude_code.cost.usage` from `sourcetype=claude:code:otel`
  (session JSONL remains the source of truth for v0.2).

## Release Notes

### 0.2.0

- **Pricing is now lookup-driven**: `calculate_cost` reads `ai_model_pricing.csv` (wildcard match, first-row-wins);
  hardcoded pricing removed. Current model families (Fable 5, Opus 4.x, Sonnet 4.6, Haiku 4.5, Gemini 3.x) priced;
  unknown models are flagged `pricing_known=false` at $0 instead of being mis-priced.
- **Canonical token fields**: macros now emit `cache_read_tokens`/`cache_creation_tokens` (matching props.conf) —
  the old `cache_read`/`cache_created` macro fields are retired.
- **Null-safe dedup**: `dedup_messages` and `EVAL-dedup_key` no longer silently drop events missing `requestId`.
- **OTel semconv compliance** (breaking): `gen_ai.usage.input_tokens` now includes cached tokens.
- **`ai_all_indexes`** (breaking): recomposed from per-provider macros; `index=ai` removed.
  Override the per-provider macros in `local/` if your indexes differ.
- New macros: `claude_metric_events`, `openai_index`, `copilot_index`.
- 10 new sourcetypes: `vscode:{logs,settings,extensions}`, `copilot:chat:otel`, `github:copilot:usage`,
  `gemini:cli:otel`, `macos:{unified_log,system:metrics,system:thermal,power:battery}`.
- `TIME_PREFIX` added to `claude:code:session`/`claude:code:subagent` for direct-ingest paths.

## References

- [OTel GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Anthropic-specific conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/anthropic/)
- [ccusage](https://github.com/ryoppippi/ccusage) - Token model reference
- [Splunk CIM](https://help.splunk.com/en/splunk-enterprise/common-information-model/5.3/data-models/cim-fields-per-associated-data-model)
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)

---

> Part of a [larger ecosystem of ~40 repos](https://docs.jacobpevans.com) — see how it all fits together.
