# llm-gateway

The LLM gateway is the one route to the brain: it authenticates, attributes, budgets, meters and audits every model call. Agent hosts call it with an ordinary OpenAI-shaped client, on port 4000 in the platform namespace.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-llm-gateway` | `python -m llm_gateway` | `REGISTRY_DSN`; no provider key | `budget_ledger`, `audit_events` |

The provider key lives one hop behind, in the `llm-proxy` pod (LiteLLM, or the [mock brain](../mock-brain/README.md)). A NetworkPolicy lets only this gateway reach it.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The FastAPI app: the auth middleware, the completion routes, the one pipeline (`_complete`), cost and usage parsing, the error shape. |
| `config.py` | `LlmGatewaySettings`: the shared workload-authn fields, `upstream_url` and `model`. The chart sets all of them. |
| `__main__.py` | Runs the app with uvicorn on `common.topology.LLM_GATEWAY_PORT`. |

## How it works

### Every call is attributed before it is forwarded

The gateway serves both OpenAI surfaces: `POST /chat/completions` and `/v1/chat/completions`, and the Responses API at `/responses` and `/v1/responses`. Only `/healthz` and `/health/liveliness` are open. Every completion takes the same pipeline:

```
workload authn       common.authn, as at the MCP gateway
-> attribution       X-Poseidon-Run-Id names a known run of the caller's company
-> budget headroom   the run's ledger SUM against the per-run cap
-> pin the model     MODEL overwrites the request's model field
-> forward           to UPSTREAM_URL (llm-proxy)
-> meter and audit   one ledger row, one audit event
```

The agent host sets the run header in `agent_host.procedures.build_llm`. A refusal is an OpenAI-shaped error body, `{"error": {"message", "type": "poseidon_policy", "code"}}`, so standard clients raise it with the code intact. Each outcome is one audit event whose `rule` is the code below:

| Status | `rule` | When | Decision |
|---|---|---|---|
| 400 | `llm_unattributed` | No run header. | deny |
| 403 | `llm_foreign_run` | An ephemeral agent names a run other than its own. | deny |
| 403 | `llm_unknown_run` | The run is unknown, or belongs to another company. | deny |
| 429 | `budget_exhausted` | The run's spend has reached its cap. | deny |
| 502 | `llm_cost_unreported` | The upstream answered 200 without a usable cost. | deny |
| 502 | `llm_upstream_error` | The upstream is unreachable. | allow |
| upstream's | `llm_upstream_status` | The upstream answered non-200; its body is passed through. | allow |
| 200 | `budget_headroom` | Served and metered. | allow |

The last four carry `backend_status` (`error`, or `ok` when served). The event's tool is `llm.chat_completions` or `llm.responses`. Its `args_digest` covers the model and the message or input-item count, never the prompt text.

### The budget is enforced here

Before forwarding, the gateway reads the compiled spec's `budgets.per_run_usd` (the blueprint's `budgets.per_run.usd`) from `companies`. It compares it with `BudgetLedger.spent_for_run`. At or over the cap, the call is refused with `budget_exhausted`. A cap of zero or none means uncapped, and the call is still metered.

The gateway has no Temporal client. The implement workflow reads `budget_exhausted` in the error the agent host surfaces and parks the run as `parked-budget_exhausted`. See [Gates and runs](../../docs/how-to/gates-and-runs.md).

The chart's `run.model` reaches the gateway as `MODEL` and replaces every request's model field. An agent cannot choose a more expensive model.

### Spend is what the provider reports

On a 200, the gateway reads the cost from the `x-litellm-response-cost` header and records it verbatim. Zero is a price and is recorded. A missing, negative, non-finite or unparseable cost withholds the completion as `llm_cost_unreported`, and nothing is metered. Upstream failures are not metered either.

Usage maps both vocabularies onto the ledger's columns. Chat prompt and completion tokens, or Responses input and output tokens, land in `prompt_tokens` and `completion_tokens`. Cached and reasoning tokens come from the details blocks and are zero when absent.

Each ledger row carries the company, run, agent, parent agent, role, model, tokens and USD. This gateway is the ledger's only writer, so any slice of spend is one `SUM`. The mock brain reports the same header, so metering runs end to end without a paid brain.

## Invariants

All tests are in `tests/test_llm_gateway.py`.

- No token, no call: `test_unauthenticated_call_is_rejected`.
- No run header, no call, and the refusal is audited: `test_unattributed_call_is_rejected_and_audited`.
- A caller bills only a known run of its own company: `test_unknown_run_is_rejected`.
- An ephemeral agent bills only its own run: `test_ephemeral_caller_cannot_bill_a_foreign_run`.
- The deployment's model answers: `test_the_deployment_model_is_pinned_and_metered`.
- The cap refuses with a distinguishable error and no new spend: `test_exhausted_cap_refuses_with_a_distinguishable_error`, `test_cap_bites_only_at_the_cap`.
- An uncapped company is metered and never refused: `test_uncapped_company_is_not_blocked`.
- An unpriced completion is withheld and never metered: `test_unpriced_completion_is_withheld_and_never_metered`, `test_garbled_cost_header_is_withheld_too`.
- Zero cost is recorded: `test_zero_cost_is_a_price_and_is_recorded`.
- The Responses surface keeps the same contract and never digests prompt text: `test_responses_forwards_the_pinned_model_and_meters`, `test_responses_digest_never_contains_prompt_text`.

## Testing

```bash
uv run pytest packages/llm-gateway
```

It needs no network, cluster or Postgres (see [Contributing](../../docs/developing/contributing.md)). The registry is in memory, TokenReview is faked and the upstream is an `httpx.MockTransport`. On kind, `test_budgets.py` drives a run into `parked-budget_exhausted` and out again, and checks that unattributed calls are refused.

## Related

- [Security model](../../docs/explanation/security-model.md)
- [Use a paid brain](../../docs/how-to/paid-brain.md)
- [Blueprint reference](../../docs/reference/blueprint.md), for `budgets`
- [mcp-gateway](../mcp-gateway/README.md), [agent-host](../agent-host/README.md), [mock-brain](../mock-brain/README.md)
