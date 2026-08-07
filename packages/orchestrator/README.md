# orchestrator

The durable spine: three Temporal workflows, the activities they sequence,
the process gates they park on, the Kubernetes surface for ephemeral crew
pods, the worker that runs it all, and the `poseidon` CLI.

Two things make this package the one to read carefully. It is the **only
minter of ephemeral identities**, and it owns the **human merge gate** —
the single point where a person is required before code leaves the system.

## Layout

| Path | Owns |
|---|---|
| `workflows/` | `intake`, `implement`, `review` — sequencing, timeouts, retries and compensation. |
| `activities/` | The 22 activities, each a thin client over the gateway, the SandboxBackend, or agent HTTP. |
| `gates/` | `Gate` — a signal-driven wait point with an escalation timer, and the `process-gate` audit event shaping. |
| `crewpods.py` | The bounded Kubernetes surface for crew pods: **pods only**, in the **company namespace only**, deleted only by label selector. |
| `identity.py` | Ephemeral identity minting — the fulfill half of the identity contract. |
| `clients.py` | `GatewayClient` (MCP + admin) and `AgentClient` (dispatch/poll). |
| `cli/` | `poseidon run|review|signal|status`, `poseidon blueprint compile|preview|apply`. |
| `worker/` | The worker process. |
| `config.py` | `TemporalSettings` (all a client needs) and `WorkerSettings` (what activities need). |

## The three workflows

**intake** — goal text in, tickets and recorded crew intent out. The PM does
the scoping itself through governed tools. This is the injection surface for
the standing conductor: the goal text is untrusted input handed straight to
the PM, and containment is the compiled policy's job, not the prompt's.

**implement** — the crew is the only execution path. `fulfill_crew` stands
the ephemeral roster up, the executor is dispatched at its crew pod, and its
completion claim is *advisory*. The gate of record is `platform_verify` —
the orchestrator's own run of the sandbox test suite under its own identity.
Red verify feeds the failure back for a bounded number of re-dispatches;
only a green verify releases the separate `mode="deliver"` dispatch that
opens the PR.

**review** — PR in, human-gated merge out: read the PR, provision a sandbox
from its head ref, dispatch an ephemeral reviewer, then **park on the merge
gate**. Approve merges under the platform identity; reject or timeout is an
honest failure.

## Why delivery cannot jump the gate

Two independent mechanisms, and both have to hold:

1. The implement-mode tool surface has no `open_pr` at all.
2. The deliver dispatch is sequenced strictly after `platform_verify`.

An agent claiming success does not deliver anything. The platform re-runs
the tests itself and decides.

## The merge gate

The one human gate, and the sole owner of the merge guarantee. Merge
approval is *workflow state* — the single named exception to policy-side
defence in depth, which is why the gate's own outcomes are first-class audit
events written through an activity. Nobody home? The timer escalates at half
the deadline and expires the gate at the end. The park is API-visible via
the `state` query, so it is never a silent hang.

Gate config comes from the compiled plan (`workflows.<name>.gates`), never
hardcoded. The budget pause rides the same `Gate` spine.

## Identity minting and revocation

Agent ids are deterministic from `{crew, member, run}`, so a mint retried
after a worker death lands on the same participants row instead of leaking a
second identity. The token is injected into the crew pod's env **at
provision time**, which is what keeps it out of Temporal's workflow history.

Revocation is a registry row termination — effective before the TTL runs
out — and it runs on *every* terminal path, because `teardown` is the one
compensation anchor Temporal guarantees.

`ParticipantRevoked` is raised rather than swallowed when a mint would
revive a swept row: that is a retry that lost the race with its run's
teardown, and it must abandon the identity rather than build on it.

## Entry points

| What | How | Image |
|---|---|---|
| worker | `python -m orchestrator.worker` | `Dockerfile.orchestrator` |
| CLI | `uv run poseidon …` | run from a checkout |

```
uv run poseidon blueprint compile examples/hello-saas.yaml --no-live --out ./bundle
uv run poseidon blueprint preview examples/hello-saas.yaml
uv run poseidon blueprint apply examples/hello-saas.yaml --yes

uv run poseidon run examples/task-happy.md               # implement by default
uv run poseidon run --workflow intake examples/goal-injected-conductor.md
uv run poseidon review 41                                # start a review run for PR #41
uv run poseidon signal <run_id> merge --workflow review  # approve the merge gate
uv run poseidon signal <run_id> merge --no-approved --workflow review
uv run poseidon status <run_id>
```

`run` and `review` take the task file and the PR number as positional
arguments; `signal` takes the run id and the gate name, with the verdict on
`--approved/--no-approved`.

The CLI holds no credentials — runs execute in the in-cluster worker; it
only starts, signals and inspects. It reaches Temporal via
`TEMPORAL_ADDRESS` (default `127.0.0.1:7233`, i.e. a port-forward).

## Credential posture

The worker holds the egress-proxy HMAC key, the platform JWT signing key and
the registry DSN — **no `GIT_TOKEN`, no provider keys**. Its gateway
identity is an audience-scoped projected service-account token, distinct
from the automounted API-server token the k8s_jobs backend uses: each is
useless where the other is needed.

## Cross-package interactions

- `gateway` for every governed call, including its own platform-lifecycle
  ones (`repo.archive`, sandbox registration, `sandbox.run_tests`,
  `git.merge_pr`).
- `sandbox` for the `SandboxBackend` contract and the k8s_jobs backend.
- `registry` for runs, participants, crew requests and companies.
- `compiler` + `schema` behind the `poseidon blueprint` commands.
- `agent` over HTTP for dispatch.

## Tests

```
uv run pytest packages/orchestrator
```

Unit tests cover activity behaviour and retry safety against fakes.
Sequencing, compensation and durability are workflow concerns and are proven
live by the kind suite — including killing the worker mid-run and asserting
Temporal replays it with nothing leaked.

## Invariants

- Workflows reference activities **by name**; this module imports nothing
  from the activity implementations. That is the seam an engine swap would
  use.
- Workflow code cannot touch the registry (Temporal's sandbox), so every
  audit write goes through an activity.
- Teardown runs on success, failure, cancel, activity timeout and worker
  death.
- Fulfillment never improvises authority: without a recorded, unexpired
  crew request it refuses. When a workflow records one itself, it is
  allowed only if the compiled decision the `crew.request` *tool* would
  face allows it — same bundle, same enriched input.
- Crew pod deletes always go through the `{run_id, kind=crew-agent}` label
  selector, so nothing unlabeled — and no sandbox pod sharing the run label
  — can ever be touched.
- `activeDeadlineSeconds` on crew pods: the teardown race guard refuses to
  provision for a finished run, but a pod created in the last instant before
  that becomes true has no owner left to collect it.
