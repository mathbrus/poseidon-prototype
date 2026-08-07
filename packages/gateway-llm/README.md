# gateway-llm

The LLM gateway: the one route to any model, and the second of the two
credential-holding chokepoints. Where the MCP gateway governs what an agent
can *do*, this governs what an agent can *spend* — and both verify the same
workload credentials against the same registry state.

It holds no provider keys. The upstream that does (LiteLLM in a real
deployment, the mock brain in development) is sealed by NetworkPolicy so
that only this service can reach it. That is what makes an unmetered,
unattributed model call structurally impossible rather than merely
discouraged.

## The pipeline

Every completion call:

```
workload authn (common.authn — the same credentials as the MCP gateway)
-> run attribution: X-Poseidon-Run-Id must name a known run of the
   caller's company; ephemeral callers must be bound to exactly that run
-> budget headroom: the applied blueprint's per-run cap vs. the ledger SUM
-> model-tier routing: the caller's instance tier picks the model
-> forward upstream, then meter the usage to the ledger and audit
```

Anonymous or unattributed passthrough does not exist. A call with a valid
workload identity but no run context is refused.

## Budget enforcement is local, and loud

An exhausted budget returns a distinguishable `budget_exhausted` error and
is audited. It is never silent degradation — no quietly smaller model, no
truncated context.

Enforcement lives here rather than in the workflow because this is the point
every model call passes through; there is no Temporal client in this
service. The orchestrator observes the stop through the error the brain
surfaces, and turns it into an API-visible `paused-budget_exhausted` state
with a real escalation event.

## Tier routing is the gateway's, not the brain's

The blueprint names *tiers* (`frontier`, `standard`, `small`); only the
deployment knows which concrete model a tier means. The caller's requested
model field is overridden. An agent cannot upgrade itself to a more
expensive model by asking.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The FastAPI app and the whole pipeline above. |
| `config.py` | Settings: the shared workload-authn shape plus tier→model config and the per-1K-token price the ledger accrues at. |
| `__main__.py` | The entry point. |

## Entry points

```
python -m gateway_llm      # listens on LLM_GATEWAY_PORT, default 4000
```

`Dockerfile.gatewayllm`. Port 4000 is the port agents already expect for
model access, so pointing them here is a URL change and nothing else.

## Cross-package interactions

- `common.authn` — the same `Authenticator` the MCP gateway uses.
- `registry.BudgetLedger` for metering and the headroom SUM;
  `registry.Runs` and `Companies` for attribution and the per-run cap;
  `registry.AuditStore` for `source=llm-gateway` events.
- `agent` builds its LLM client against this service.
- `mockllm` is the upstream in development; LiteLLM is the upstream in a
  real deployment. Neither is reachable except from here.

## Tests

```
uv run pytest packages/gateway-llm
```

Runs the pipeline against in-memory registry doubles and a stubbed
upstream: unattributed calls rejected, attribution verified, tier routing
applied, every forwarded call metered and audited, and the per-run cap a
hard stop with a distinguishable error.

## Invariants

- One ledger with dimensions, never parallel accounts, so crew fan-out
  stays reconcilable: every entry carries `{run, parent, participant, role}`
  plus company and model.
- The single `llmApiKey` Helm value is written to both `OPENAI_API_KEY` and
  `ANTHROPIC_API_KEY` in the upstream's secret. That is intentional —
  LiteLLM reads the provider-specific name matching the model prefix — not
  a leak of one key into two places.
- Pricing is flat per 1K tokens. Per-model price tables are a future
  concern; what matters is that spend accrues from real usage and the cap
  bites.
