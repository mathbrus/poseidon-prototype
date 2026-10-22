# Console

The console is the one service a person acts on a company through. For
every act, it verifies who you are, asks policy whether you may, records
the act under your name, and only then does it. The `poseidon` CLI is a
client of this API, and so is the acceptance suite. Nothing else is meant
to reach the platform.

On kind the console listens on `http://127.0.0.1:8090`
(`POSEIDON_CONSOLE_URL`).

## Who a human is

Every request except `/healthz` carries a bearer token that the Kubernetes
API server issued for the console's audience, `poseidon-console`. The
console checks it with the TokenReview API.

- **On kind**, a person is a ServiceAccount the chart declares,
  `human-<name>` in the platform namespace, one per entry of the chart
  value `console.humans`. `poseidon login <name>` mints a token for it with
  your kubeconfig.
- **With an identity provider**, the API server's own user is the person,
  and nothing needs declaring.

A person's token works only at the console, and an agent's token is
refused there. Each refusal is recorded.

## What a person may do

Signing in lets a person read. Acting needs a name in the blueprint's
`company.humans`. The compiler turns that list into one rule per person
and act, so revoking someone is a diff. A person the blueprint doesn't
name is refused with `human_not_recognized`, under their name.

The first apply of a company is the one act no policy can decide, because
no policy exists yet. Any verified person may make it, and the audit row
records `blueprint_apply_bootstrap`.

The acts, rendered from `common.audit`:

<!-- gen: console-acts -->

## Verdicts

`POST /runs/{run_id}/gates/{gate}` answers only a run parked on that gate.
The console records the verdict and your reason before it signals the
workflow, and the merge reads that record back
([The merge gate](../explanation/merge-gate.md#when-you-resolve-it)).

## Requests

JSON in, JSON out. Acts are `POST` or `DELETE`, reads are `GET`. Every act
that commits the company to something takes a `reason`, recorded as the
audit row's rationale.

```bash
TOKEN=$(uv run poseidon login owner)

# Read the company
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/company

# Approve a parked merge
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"approved": true, "reason": "engine first"}' \
  http://127.0.0.1:8090/runs/auto-3d73e674/gates/merge

# Pause the company
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "reading the ledger"}' \
  http://127.0.0.1:8090/heartbeat/pause
```

| Body | Used by |
|---|---|
| `{"blueprint": "<yaml text>"}` | `POST /blueprint/preview`, `POST /blueprint/apply` |
| `{"workflow": "implement", "number": 42}` | `POST /runs` |
| `{"approved": true, "reason": "…"}` | `POST /runs/{run_id}/gates/{gate}` |
| `{"reason": "…"}` | `POST /runs/{run_id}/cancel`, `POST /heartbeat/pause`, `POST /heartbeat/resume` |
| `{"reason": "…", "wait": true}` | `DELETE /company` |

To read back your own writes, take a timestamp from `GET /now` before you
act, then pass it as `?since=` to `/runs`, `/spend` or `/heartbeat`.

## Routes

<!-- gen: console -->

## Status codes

| Code | Body | Meaning |
|---|---|---|
| `200` | the result | Done. |
| `401` | `{"detail": "unauthorized"}` | No token, or a token the console can't verify. |
| `403` | `{"detail": "…", "rule": "human_not_recognized"}` | Policy refused the act. `rule` names the rule. |
| `404` | `{"detail": "no company is applied"}` | The route needs a company and none is applied. |
| `422` | `{"detail": "no run 'x'"}` | The request can't be acted on: an unknown run, a run not parked on that gate, a blueprint that doesn't validate, or another company's name. |
| `503` | `{"detail": "temporal unreachable: …"}` | The workflow engine didn't answer. |

## What the console holds

The registry's connection string and its own token for TokenReview. It
holds no GitHub token, no model key and no signing key. In the cluster,
it may create, update and delete the `Company` and `AgentInstance`
resources, and read the company namespace. Its NetworkPolicy lets it reach OPA,
Postgres, the Temporal frontend, DNS and the Kubernetes API, and nothing
else.

It writes the audit table and the pause, and nothing else in the registry.
