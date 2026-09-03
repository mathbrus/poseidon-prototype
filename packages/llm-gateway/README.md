# llm-gateway

The LLM gateway: the one route to any model. Where the MCP gateway
([`gateway`](../mcp-gateway/README.md)) governs what an agent can *do*, this
service governs what an agent can *spend* — and both verify the same
workload credentials against the same registry state, with the same
`Authenticator`.

One asymmetry with its sibling is worth stating precisely. The MCP
gateway holds `GIT_TOKEN` and acts with it; this gateway holds no
provider key at all. The key lives one hop behind it, in the `llm-proxy`
pod — LiteLLM in a real deployment, the deterministic mock brain in
development — and a NetworkPolicy admits exactly one caller to that pod:
this service. The gateway's job is everything a raw proxy would skip:
authenticate, attribute, budget, route, meter, audit.

The split is deliberate. Folding the key into this
service would delete a Deployment but not the mass behind it: the proxy
is also the layer that translates one OpenAI-shaped wire format into
whatever the configured provider actually speaks, and pulling that (plus
the key, plus external egress) into the policy-holding chokepoint trades
one small pod for a fatter trusted computing base. Small enough to
inspect wins.

## Why model access is a chokepoint at all

Tool calls are not the only thing a compromised agent can abuse. The
root threat model ([README §1](../../README.md#1-the-problem)) lists
**spend** — denial-of-wallet — as a goal in its own right, and the
provider API key is precisely the kind of credential an injected agent
would exfiltrate. Both problems have the same shape as the git-token
problem, so they get the same answer: the agent never holds the
credential, and every call passes a service that decides first.

Putting the chokepoint here buys four things at once. The key is
unstealable, because no agent pod contains it. Every completion is
*attributed*, so "what did run X cost" is one query. The per-run budget
is *enforceable*, because there is no unmetered path to a model. And the
concrete model served is the *deployment's* choice, not the agent's.

To the agent all of this is invisible. The gateway speaks plain OpenAI
on port 4000 — the port agents already expect for model access — on
**both** OpenAI surfaces: chat completions and the Responses API
(`/responses`, `/v1/responses`). SDKs auto-select Responses for models
whose metadata lists it (gpt-5\*, with OpenHands' default
`api_mode="auto"`), so the second surface is what keeps "pointing an
off-the-shelf SDK at governed model access is a URL change and nothing
else" true for current OpenAI models. The agent host builds an ordinary
LiteLLM-style client whose `base_url` points here
(`agent.procedures.build_llm`).

## The pipeline

Every completion call (`POST /chat/completions`, `/v1/chat/completions`,
`/responses` or `/v1/responses`; the health probes are the only
unauthenticated routes):

```
workload authn (common.authn — the same credentials as the MCP gateway)
-> run attribution: X-Poseidon-Run-Id must name a known run of the
   caller's company; ephemeral callers must be bound to exactly that run
-> budget headroom: the applied blueprint's per-run cap vs. the ledger SUM
-> model-tier routing: the caller's instance tier picks the model;
   the request's own model field is overridden
-> forward upstream, then meter the usage to the ledger and audit
```

Anonymous or unattributed passthrough does not exist. A call with a
valid workload identity but no run context is refused.

## How a completion is attributed

The agent host injects `X-Poseidon-Run-Id` into every request. The
gateway does not take it on faith:

- No header at all → `400`, code `llm_unattributed`, audited.
- An ephemeral caller naming a run other than the one its live
  `run_participants` row binds it to → `403`, `llm_foreign_run`. An ephemeral
  agent cannot bill a foreign run.
- A run the registry has never seen, or one belonging to a different
  company than the caller's → `403`, `llm_unknown_run`.

Only then does the call proceed, and when it succeeds the ledger row
carries the full genealogy: `{company, run_id, agent_id,
parent_agent_id, role, model, prompt_tokens, completion_tokens, usd}`.
`agent_id` is the verified caller; the parent is the standing
conductor recorded in its `run_participants` row. That is what keeps
crew fan-out reconcilable — one ledger with dimensions, never parallel
accounts, so any slice (per run, per role, per parent) is a `SUM`.

The audit trail deliberately records less. The event's `args_digest` is:

```python
# Digest, never content: audit must not become a leak channel.
digest = hashlib.sha256(
    json.dumps(
        {"model": body.get("model"), "messages": len(body.get("messages", []))},
        sort_keys=True,
    ).encode()
).hexdigest()
```

Prompt text never reaches the audit store. A prompt is exactly the kind
of thing that contains workspace contents, and the audit store must not
become the exfiltration channel it exists to expose. Responses bodies
carry `input` items instead of `messages`, so that surface digests
`{model, input-item count}` — same rule, same reason. The two surfaces
are distinguishable in the trail by the event's tool name:
`llm.chat_completions` vs `llm.responses`. Metering differs only in
vocabulary: Responses `input_tokens`/`output_tokens` land on the same
ledger prompt/completion columns.

## Budget enforcement is local, and loud

Before forwarding, the gateway reads the applied company spec's
`budgets.per_run_usd` and the ledger's spend for this run. At or over
the cap, the call is denied with `429` and a distinguishable error, and
the denial is itself an audit event (`decision=deny`,
`rule=budget_exhausted`, `source=llm-gateway`):

```python
return JSONResponse(
    {"error": {"message": message, "type": "poseidon_policy", "code": code}},
    status_code=status,
)
```

The OpenAI-shaped body is the load-bearing detail: standard clients
raise it with the code and message intact, which is what lets the agent host
surface `budget_exhausted` verbatim and the workflow recognise it.

Enforcement lives here rather than in the workflow because this is the
point every model call passes through; there is no Temporal client in
this service. The orchestrator observes the stop through the error the
agent host surfaces: the implement workflow matches the `budget_exhausted`
marker in the dispatch result, parks on a `budget` signal in an
API-visible `paused-budget_exhausted` state, fires an escalation at half
the gate deadline, and records every transition as a `process-gate`
audit event. `poseidon signal <run_id> budget --approved` (after
raising the cap) re-dispatches; anything else finalizes the run with
the budget error. Never a quietly smaller model, never a truncated
context — exhaustion is a distinguishable error, an audit event, an
escalation, and a visible paused state.

Two edges keep the semantics honest. A cap of zero (or none) means
*uncapped*: the call flows and metering continues, because metering and
enforcement are separable — silence is not. And the workflow also
checks headroom once at run creation ("born with headroom or not at
all"), but that is an observation at an activity boundary; the
enforcement point stays here.

## Tier routing is the gateway's, not the agent's

The blueprint never names a model. Roles in the catalog name
*tiers* — `small`, `standard`, `frontier` — and only the deployment
knows which concrete model a tier means, via the `TIER_MODELS` JSON
env (`{"frontier": "gpt-5.6-luna", ...}`). The gateway looks up the caller's
`agent_instances` row, maps its tier, and **overwrites the request's
model field**. Whatever model the agent asked for is advisory; an agent
cannot upgrade itself to a more expensive model by asking.

The honest edge: instance rows are written by the operator for the
standing agents, so an ephemeral agent has none and resolves to
`DEFAULT_MODEL`, as does any unmapped tier. In the kind deployment the
mock overlay maps every tier to `mock-brain` anyway; per-agent tier
routing for ephemeral agents is future work the config comment admits
("for unmapped tiers and callers without an instance row").

## Metering

On a 200 from upstream, the gateway accrues the provider-computed cost
from the `x-litellm-response-cost` response header to the ledger,
verbatim, together with the usage block's token breakdown (prompt,
completion, cached, reasoning), then writes an `allow` audit event
(`rule=budget_headroom`, `backend_status=ok`). The mock brain reports
the same header and details deterministically, so the whole path is
exercised end to end without a paid provider.

A 200 whose cost header is missing, negative, or unparseable is
withheld: `502` (`llm_cost_unreported`), audited as a `deny` with
`backend_status=error`, nothing metered, the completion never
delivered. Zero is a price (free models exist) and is recorded. An
unreachable upstream returns `502` (`llm_upstream_error`); a non-200 is
passed through verbatim — both audited with `backend_status=error` and
*not* metered, because nothing was served. Spend accrues from what the
provider reports, never from guesses.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The FastAPI app and the whole pipeline above. |
| `config.py` | Settings: the shared workload-authn shape plus tier→model config. |
| `__main__.py` | The entry point. |

## Entry points

```
python -m llm_gateway      # listens on LLM_GATEWAY_PORT, default 4000
```

Built as `poseidon-llm-gateway` by `deploy/build-images.sh`; deployed by the chart's
`llm-gateway.yaml`, which also grants the ServiceAccount
`system:auth-delegator` so TokenReview works. The upstream defaults to
`http://llm-proxy:4000`, and the forward client allows a 600-second
read — a real model legitimately thinks for minutes.

## Cross-package interactions

- `common.authn` — the same `Authenticator` the MCP gateway uses:
  TokenReview for every caller, with standing agents resolved through
  `agent_instances` and ephemeral agents through a live
  `run_participants` row.
- `registry.BudgetLedger` for metering and the headroom SUM (this
  gateway is the ledger's only writer); `registry.Runs` and `Companies`
  for attribution and the per-run cap; `AgentInstances` for the tier;
  `registry.AuditStore` for `source=llm-gateway` events.
- `agent` builds its LLM client against this service and injects the
  run header (`agent.procedures.build_llm`).
- `orchestrator` turns the surfaced `budget_exhausted` error into the
  paused state, escalation, and `process-gate` audit events.
- [`mockllm`](../mockllm/README.md) is the upstream in development;
  LiteLLM is the upstream in a real deployment. Neither is reachable
  except from here.

## Tests

```
uv run pytest packages/llm-gateway
```

The suite runs the pipeline against in-memory registry doubles
(`registry.memory`) and a stubbed OpenAI-shaped upstream: rejected
without authn, rejected and audited without attribution, unknown and
foreign runs refused, tier routing overriding the requested model,
every forwarded call metered and audited, the cap a hard stop with a
distinguishable error and no new spend, spend accruing call by call
below the cap, and an uncapped company metered without being stopped.
The Responses surface is held to the same contract: routing, metering,
every guard and the hard stop over `/responses`, with digests that
never carry prompt text.

## Invariants

- One ledger with dimensions, never parallel accounts, so crew fan-out
  stays reconcilable: every entry carries `{run, parent, agent,
  role}` plus company and model.
- Audit digests, never prompt content: the audit store must not become
  a leak channel.
- The single `llmApiKey` Helm value is written to both `OPENAI_API_KEY`
  and `ANTHROPIC_API_KEY` in the upstream's secret. That is intentional
  — LiteLLM reads the provider-specific name matching the model prefix
  — not a leak of one key into two places.
- The gateway holds no price. The upstream proxy computes each call's
  cost and reports it; a completion the proxy cannot price is withheld
  rather than guessed at or given away.
