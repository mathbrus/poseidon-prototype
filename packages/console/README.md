# console

The human's chokepoint: the one service a person acts on a company through,
and the counterpart of the [MCP gateway](../mcp-gateway/README.md) for
agents. A gateway asks "may this workload call this tool?"; the console
asks "may this person perform this act?" — and both get the answer from the
same compiled bundle, audit it in the same store, and do nothing without
it.

Every request but the health probe carries a token the Kubernetes API
server issued for the console's own audience (`poseidon-console`), verified
through TokenReview by the shared `common.authn.Authenticator` in human
mode: on a cluster without an identity provider the person is a
`human-<name>` ServiceAccount the chart declares; elsewhere the API server's
own user. The registry is never consulted for who a person is. Whether
they may act on the company is policy's question, compiled from the
blueprint's `company.humans`, asked per act.

## Module map

| Path | Owns |
|---|---|
| `app.py` | The FastAPI app and its route table (`ROUTES`, rendered into [the console reference](../../docs/reference/console.md)); the shared auth middleware; the error shapes (403 with the denying rule, 422 for what cannot be acted on, 503 when Temporal is unreachable). |
| `service.py` | Every act as verify → decide → do → audit, and the reads beside them. The bootstrap rule for a company's first apply. |
| `temporal.py` | The Temporal client: start, signal, cancel, inspect — addressed by `common.workflows`, shared with the worker. |
| `crs.py` | The blueprint apply: the Company and AgentInstance CRs, and nothing else — every real object is the operator's. |
| `config.py` | `ConsoleSettings`: the company, OPA, Temporal, the declared humans. |

## The acts

| Act | Decision | What happens |
|---|---|---|
| apply a blueprint | `blueprint.apply` | compile against the live registry, decide, submit the CRs; the operator converges |
| resolve a gate | `gate.resolve` | decide, **record the verdict under the person's name**, then signal the workflow |
| start a run by hand | `run.start` | decide, start `implement`/`review` on a ticket or pull request the company produced |
| cancel a run | `run.cancel` | decide, cancel the workflow; teardown compensates |
| wake the heartbeat | `heartbeat.wake` | decide, nudge the standing loop; the tick that follows is authorized like any other |

The verdict row comes before the signal, on purpose: it is the artifact
the review workflow reads back (`read_human_verdict`) and `merge.resolve`
decides on. A signal that reaches the workflow by any other road — network
reach to Temporal — finds no recorded verdict and releases nothing.

The first apply of a company is the one act policy cannot decide: no
bundle exists before the Company CR installs one. When OPA serves no
policy for the company, any verified human may apply, and the audit row
says `blueprint_apply_bootstrap`. Every later act is policy's.

## Entry points

| What | How | Image |
|---|---|---|
| service | `uvicorn --factory console.app:create_app --port 8090` | `poseidon-console` |
| client | `uv run poseidon …` ([the CLI](../cli/README.md)) | from a checkout |

## Credential posture

The registry DSN and its own projected token (for TokenReview) — no
`GIT_TOKEN`, no provider key, no signing key. Its cluster authority is
exactly what an apply needs: create/update/delete on the two CRDs and a
look at the company namespace, which the operator deliberately lacks.
NetworkPolicy lets it reach OPA, Postgres, Temporal's frontend, DNS and
the API server, and nothing else.

## Tests

```
uv run pytest packages/console
```

Everything runs against fakes: a scripted TokenReview, in-memory registry
accessors, a recording Temporal double, a recording CR submit. What is
pinned: the health probe is the only open route; a rejected token is
audited with its principal; a denied act is a 403 naming the rule and
does nothing; the verdict row precedes the signal, under the rule the
registry reads it back by; a verdict is refused unless the run is parked
on that gate; the bootstrap apply; reads need a human and touch nothing;
the route table equals the app.

## Invariants

- No act without a decision, no decision without an audit row under the
  person's name.
- The verdict is recorded before it is delivered.
- The console writes `audit_events` and nothing else in the registry; the
  operator writes `companies` from the CR the console submits.
- A cluster hosts one company. The console learns which from the registry
  (the operator records the applied Company CR) and refuses a blueprint
  naming another until that company is deleted; before the first apply,
  any name applies.
