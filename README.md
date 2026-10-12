# VisiCore TA for AI Observability

Splunk Technology Add-on providing field extractions, OTel semantic convention aliases, and search macros for AI coding tool observability.
Companion to [VisiCore_App_for_AI_Observability](https://github.com/JacobPEvans/VisiCore_App_for_AI_Observability).

## Architecture

```text
Filesystem -> Cribl Edge -> Cribl Stream -> Splunk HEC -> Splunk Enterprise
   (JSON)      (packs)      (routing)       (port 8088)    (index=claude)
```

The daily Cribl Stream REST collector sends LiteLLM's official
`model_prices_and_context_window.json` file as one HEC event to `index=llm_pricefeed`,
`sourcetype=litellm:pricefile`. The `LiteLLM price file to lookup` search runs
daily at 04:30 server time, reads the newest feed event from the prior 30 hours, and writes the app
lookup `lookups/litellm_model_prices.csv`. The lookup keeps models with input and output rates; cache
rates are retained when present.

## Knowledge Objects

- **props.conf** - Field extractions for 30 sourcetypes (Claude, Gemini, Antigravity, VS Code, GitHub Copilot, macOS)
- **macros.conf** - 13 reusable search macros (index filters, base filters, dedup, token extraction, reported-cost handling, tools, cache)
- **eventtypes.conf** - 7 event types for Claude, Gemini, and Copilot events
- **tags.conf** - ai/llm/genai tags for CIM compliance

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

## Cost Fields

`calculate_cost` uses numeric `response_cost` when present, including LiteLLM spend events with
`sourcetype=litellm:spend`. Otherwise it matches `model` to an exact key using case-insensitive comparison against
`litellm_model_prices` and multiplies the four token counts by their per-token rates. An unknown model,
or a missing cache rate when that cache count is positive, produces a null `cost_usd` and
`pricing_known=false`.

## Macros

| Macro | Purpose |
|---|---|
| `claude_index`, `gemini_index`, `openai_index`, `copilot_index` | Per-provider index filters (override in `local/` to relocate) |
| `ai_all_indexes` | All provider indexes, composed from the four macros above |
| `claude_assistant_events` | Claude assistant messages (the costed events) |
| `claude_all_session_events` | Session + subagent events |
| `dedup_messages` | Null-safe dedup by `messageId:requestId` |
| `extract_tokens` | Canonical 4-token convenience fields + `total_tokens` |
| `calculate_cost` | Reported `response_cost` or LiteLLM per-token lookup pricing; leaves unpriced events null (includes `extract_tokens`) |
| `claude_metric_events` | **Canonical Claude session base**: assistant events, deduped, with tokens, `cost_usd`, and `pricing_known` |
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

Ensure indexes exist: `claude`, `gemini`, `ai_usage`, `llm_pricefeed` (plus `vscode`, `openai`, `mac_perf`, `os` if those feeds are enabled).

## Usage

Use the composed macro for Claude session analysis:

```spl
`claude_metric_events` | timechart sum(total_tokens) as tokens, sum(cost_usd) as cost
```

Use LiteLLM spend events for per-request cost totals:

```spl
index=ai_usage sourcetype="litellm:spend" | `calculate_cost` | stats sum(cost_usd) as cost, count as requests by model
```

Claude session events use the daily LiteLLM model lookup when `response_cost` is absent. Searches that
sum or average `cost_usd` ignore null event costs, and `pricing_known` identifies unpriced events.

## Packaging

```bash
./scripts/package.sh
```

Produces a versioned tarball in `build/`.

## Roadmap

- v0.3: OTel-metrics cost source variant using `claude_code.cost.usage` from `sourcetype=claude:code:otel`.

## Release Notes

### 0.2.0

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

---

> Part of a [larger ecosystem of ~40 repos](https://docs.jacobpevans.com) — see how it all fits together.
