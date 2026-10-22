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
that commits the company to something takes a `rationale` (the CLI's `--reason`),
recorded beside the act's audit row.

```bash
TOKEN=$(uv run poseidon login owner)

# Read the company
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/company

# Approve a parked merge
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"approved": true, "rationale": "engine first"}' \
  http://127.0.0.1:8090/runs/auto-3d73e674/gates/merge

# Pause the company
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"rationale": "reading the ledger"}' \
  http://127.0.0.1:8090/heartbeat/pause
```

Every request and response body is a model of the
[console interface](#the-console-interface).

To read back your own writes, take a timestamp from `GET /now` before you
act, then pass it as `?since=` to `/runs`, `/spend` or `/heartbeat`.

## Routes

<!-- gen: console -->

## Status codes

| Code | Body | Meaning |
|---|---|---|
| `200` | the route's response model | Done. |
| `401` | `Problem`: `{"detail": "unauthorized"}` | No token, or a token the console can't verify. |
| `403` | `PolicyRefusal`: `{"detail": "…", "rule": "human_not_recognized"}` | Policy refused the act. `rule` names the rule. |
| `404` | `Problem`: `{"detail": "no company is applied"}` | The route needs a company and none is applied. |
| `422` | `Problem`: `{"detail": "no run 'x'"}` | The request can't be acted on: a body that doesn't validate, an unknown run, a run not parked on that gate, a blueprint that doesn't compile, or another company's name. |
| `503` | `Problem`: `{"detail": "temporal unreachable: …"}` | The workflow engine didn't answer. |

## The console interface

The routes, their request and response models and the closed sets
they use are one interface, `common.console`, which every client is
built on: the CLI, the acceptance suite and the web client to come. The
console maps its registry rows, Temporal's answers and the Company CR
onto it in one place, so those can change without a client noticing.
Every model refuses fields it doesn't name.

The interface is held still three ways:

- **The snapshot.** `packages/console/openapi.json` is the console's
  OpenAPI document, committed. CI renders it again and fails on any
  difference, so a change to the interface is a reviewed diff in the
  same pull request.
- **The golden examples.** `packages/console/examples/` holds one
  request and answer per route, rendered from a console over in-memory
  fakes. The CLI's tests replay them, so a change the CLI doesn't follow
  fails its tests.
- **The breaking check.** `scripts/console_interface.py --breaking master`
  says whether a branch's interface is `breaking` (a route or field
  removed, a field retyped, a request field newly required, an enum
  narrowed), `additive` or `unchanged`. The release proposal reports the
  same since the last release.

The interface has no version of its own. The release is the version,
and `GET /healthz` answers it.

```bash
uv run python scripts/console_interface.py --write   # after changing the interface
uv run python scripts/console_interface.py --check   # what CI runs
```

<!-- gen: console-interface -->

## What the console holds

The registry's connection string and its own token for TokenReview. It
holds no GitHub token, no model key and no signing key. In the cluster,
it may create, update and delete the `Company` and `AgentInstance`
resources, and read the company namespace. Its NetworkPolicy lets it reach OPA,
Postgres, the Temporal frontend, DNS and the Kubernetes API, and nothing
else.

It writes the audit table and the pause, and nothing else in the registry.
