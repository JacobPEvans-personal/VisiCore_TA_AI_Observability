# VisiCore TA for AI Observability

Splunk Technology Add-on providing knowledge objects for AI coding tool observability.

## Structure

Repo root IS the Splunk package root. `default/` and `metadata/` are at the top level.

## Development Rules

- All config goes in `default/` only (no `local/`) for Splunk Cloud compatibility
- This TA is invisible in Splunk UI (`is_visible = false`)
- Token model follows ccusage; canonical convenience fields: input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, total_tokens
- The daily Cribl REST collector retrieves LiteLLM's official `model_prices_and_context_window.json` document; a Cribl pipeline unrolls it into one small JSON event per model with both base rates and optional cache rates. Events use `sourcetype=litellm:price` in `index=llm_pricefeed`; the `LiteLLM price file to lookup` search runs daily at 04:30 and refreshes `litellm_model_prices` from the latest per-model rates in the prior 30 hours.
- `calculate_cost` uses numeric `response_cost` when present; otherwise it prices token counts from `litellm_model_prices`. Unknown models and missing required cache rates produce null `cost_usd`.

## Companion App

Dashboards live in a separate repo: `~/git/VisiCore_App_for_AI_Observability/`

## Packaging

```bash
./scripts/package.sh
```

## Testing

1. Validate manifest: `jq . app.manifest`
2. Package: `./scripts/package.sh`
3. Deploy via ansible-splunk to Docker Splunk instance
4. Verify field extractions: `` `claude_assistant_events` | `extract_tokens` | head 5 ``
