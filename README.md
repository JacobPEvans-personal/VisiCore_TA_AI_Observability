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
- **macros.conf** - 13 reusable search macros (index filters, base filters, dedup, token extraction, lookup-driven cost calc, tools, cache)
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
