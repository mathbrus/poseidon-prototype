# orchestrator

The durable spine: four Temporal workflows, the activities they
sequence, the process gates they park on, the bounded Kubernetes surface
for ephemeral ephemeral pods, the worker that runs it all, and the `poseidon`
CLI. The [root README](../../README.md) states the promises; this package
is where they become control flow.

Two things make it the one to read carefully. It is the **only binder of
ephemeral identities** — the sole writer of `run_participants` rows and
creator of per-run crew ServiceAccounts — and it owns the **human merge
gate**, the single point where a person is required before code leaves the
system.

## Module map

| Path | Owns |
|---|---|
| `workflows/` | `intake`, `implement`, `review`, `heartbeat` — sequencing, timeouts, retries and compensation; `base.py` holds what they share (the timeout profiles, retry policies, workflow naming, the budget pause and the gate audit trail). Deterministic replay code; no I/O. |
| `activities/` | The activities: thin clients over the gateway, the SandboxBackend, agent HTTP, the registry, or OPA. Every side effect lives here. |
| `gates/` | `Gate` — a signal-driven wait point with an escalation timer. |
| `audit.py` | `record_event` — the platform's own audit events, under its identity or an ephemeral agent's, on the shared ingestion contract. |
| `ephemeral_pods.py` | The bounded Kubernetes surface for ephemeral pods: **pods and their per-run ServiceAccounts only**, in the **company namespace only**, deleted only by label selector. |
| `identity.py` | Ephemeral identity naming — the fulfill half of the identity contract; the gateways own the verify half. |
| `clients.py` | `GatewayClient` (MCP + admin routes) and `AgentClient` (dispatch/poll). |
| `cli/` | `poseidon run\|review\|signal\|status`, `poseidon blueprint compile\|preview\|apply` — documented in [INTERFACES.md](../../INTERFACES.md). |
| `worker/` | The worker process: registers all four workflows and all activities on the `poseidon-orchestrator` task queue, and runs the ensure loop that keeps each company's heartbeat alive. |
| `config.py` | `TemporalSettings` (all a client needs) and `WorkerSettings` (what activities need). |

## Workflow code is replayed, not run once

Temporal's model (primer in [root README §3](../../README.md#3-concepts-you-need))
shapes everything in `workflows/`. The engine records each activity's
result; when a worker dies, the workflow *function* is re-executed from
the top on a new worker, with recorded results substituted for the calls
it makes again. That only works if the function is deterministic — same
inputs, same decisions, every replay.

So the split is strict. Workflow code may sequence activities, hold retry
policies, park on signals, and compose text from values already in
history — `verify_feedback()` and `review_task()` are pure functions for
exactly this reason. It may **not** read the environment, do I/O, or
touch the registry (Temporal's sandbox blocks it anyway). Everything with
a side effect is an activity, and activities are where retry safety is
argued: each activity file opens with a per-activity note on why a retry
is safe (upsert, read-only, 409-reattach, idempotent destroy).

Two visible consequences:

- **Timeouts ride the workflow input**, not module constants or env: the
  right bounds depend on the *brain* behind the run, which only the
  client knows — a real LLM takes minutes per step, the deterministic
  mock answers in milliseconds. `Timeouts()` is the generous default;
  `FAST_TIMEOUTS` (the CLI's `--fast-timeouts`) makes a wedged step fail
  in seconds. Bounds are per activity, never a workflow-level execution
  timeout — that would hard-kill the run without compensation.
- **Workflows reference activities by name** (`"fulfill_crew"`, not an
  import). The workflow modules import nothing from `activities/`; that
  seam is what an engine swap would use, and it lets activities be shared
  across workflows — `check_budget` is defined once (in the intake
  module) and called by every run-shaped workflow.

## The four workflows

**intake** — goal text in, tickets and recorded crew intent out:
`create_intake_run → check_budget → intake_config → [gate slot] →
dispatch the conductor's `scope` procedure → finalize_intake`. The
conductor's address is derived from its converged instance (the operator
names its Service after the standing role), never configured. The PM does the scoping itself through governed tools
(`issues.*`, `crew.request`) — files the tickets, records the crew request,
and comments its summary on each ticket; the workflow never touches GitHub
or the crew tables directly. This is the injection surface for the standing
conductor: the goal text is untrusted input handed straight to the PM,
and containment is the compiled policy's job, not the prompt's. The gate
slot reads the blueprint's `workflows.intake.gates`: declare
`gates.intake.resolved_by: human` and the workflow parks on the `intake`
signal before dispatching (the shipped example declares no intake gate).

**implement** — the crew is the only execution path: `create_run →
check_budget → implement_config → fetch_workspace → provision_sandbox →
fulfill_crew`, then the dispatch/verify loop below, then the deliver
dispatch, then teardown. `fulfill_crew` stands the ephemeral crew up
and hands back each agent's identity and dispatch address, by crew role.

**review** — PR in, gated merge out: read the PR under the platform
identity, provision a *full* sandbox from its head ref (the reviewer runs
tests, it does not just read diffs), fulfill a review crew, dispatch the
ephemeral reviewer (the `review` procedure), journal its structured verdict,
then resolve the merge gate as the blueprint declares. `resolved_by:
human` parks on the `merge` signal; approve merges under the platform
identity, reject or timeout is an honest failure. `resolved_by: policy`
runs the platform's own layers first — the verify re-run in the review
sandbox (implement's `platform_verify`, one verification concept with two
callers), the registry-checked reviewer-independence fact, and one
evaluation of the compiled `merge.resolve` decision: allow releases the
merge, deny falls back to the human park (good work outside the
delegation is the human's call). A failed verdict or red verify feeds
the bounded rework loop: the findings become a backlog ticket filed
under the platform identity — the conductor consumes it like any other
issue — and the chain depth rides the canonical `Rework N for PR #R:`
title, read back by the platform from the failing PR's own lineage. At
the blueprint's `max_reworks` the PR escalates to the human park with a
`rework_exhausted` process-gate event instead of looping forever. With
no merge gate declared, the findings are the deliverable and the
workflow ends without merging — unattended auto-merge is not a default
anyone gets implicitly.

**heartbeat** — the company's own cadence, and the one workflow no
human starts: `heartbeat_config → heartbeat_observe → heartbeat_decide (the
PM) → heartbeat_authorize → start child run → record_tick → sleep
→ continue-as-new`. It is not run-shaped: there is exactly one per
company, `heartbeat-{company}`, and the worker's ensure loop starts it
whenever the applied blueprint declares a `heartbeat` section. Each tick
re-reads that section from the registry and stops when it is gone, so
revoking the heartbeat is a blueprint diff, effective at the next tick.
`heartbeat_observe` gathers the backlog, delivered-but-unreviewed PRs,
run history, capacity and spend — numbers, titles, URLs and attempt
counts only; issue bodies are untrusted input the implement run reads
itself. The registry names the delivered PRs; the git host is asked
whether each is still open, so a PR a human merged or closed by hand is
never offered (and the review workflow's `read_pr` refuses one anyway, as
`pr_not_open`, before a sandbox is spent on a head ref that may no longer
exist). What counts as consumed is one rule for issues and PRs alike
(`consumed`): a live run on the item, a run that concluded it (an
implement run with a `pr_url`, a review run with a `verdict` on its row —
`record_verdict` writes it beside the audit event), or `max_attempts`
runs on it. An aborted run — no PR, no verdict — leaves the item for
another try, carrying its prior attempt count into the policy input;
after teardown, an unconcluded run calls `record_attempts_exhausted`,
which at the cap writes `gate.heartbeat_attempts` / `gate_exhausted`
under that run's id and comments on the PR or issue under the platform
identity. An applied section without `max_attempts` refuses every tick
as `tick_refused:max_attempts_undeclared`.
`heartbeat_decide` dispatches the standing conductor's `decide` procedure
with that observation and gets back one action with a required
rationale, journaled as a `heartbeat.decide` audit event. The PM proposes
and the platform disposes: the action set is closed, the target must
come from the observation, and `heartbeat_authorize` evaluates the
compiled `heartbeat.start` decision at OPA before the tick starts
anything — the caps (`max_concurrent_runs`, `daily_cap_usd`, the
`may_start` list) are checked in the workflow for the honest audit
reason and again by policy for the refusal that does not depend on this
code (`max_attempts` rides as the target's prior attempts). A started
child run is abandoned, not supervised: the tick starts
work, it does not supervise it — progress and gates belong to the run. Every tick
outcome — started, idle and why, refused and why — is an audit event
under the `heartbeat-{company}` run id, and the daily-cap halt is a
`process-gate` event like a per-run budget stop.

Every run begins with `check_budget`: a run born with no headroom under
the per-run cap fails before anything is dispatched. Enforcement itself
stays local at the LLM gateway; this is the activity-boundary observation
of the same ledger.

## Why delivery cannot jump the gate

The executor's completion claim is *advisory* — nothing depends on it.
The gate of record is `platform_verify`: the orchestrator's own
`sandbox.run_tests` call under its own identity. Red verify composes the
task plus the failing output into a re-dispatch (`verify_feedback`),
bounded by the blueprint's `max_verify_retries`. Only then, in workflow
code, does delivery become reachable:

```python
if verified and not error and not (result or {}).get("error"):
    delivery = await self._dispatch(
        input.run_id, executor_url, "deliver", task_source, timeouts
    )
```

`deliver` is a *separate dispatch* whose tool surface is the implement
surface **plus `open_pr`** (procedure table in the
[agent README](../agent/README.md)). During implementation the delivery
tool does not exist in the model's conversation, so two independent
mechanisms must both fail for unverified code to ship: the implement
procedure's toolset has no `open_pr`, and the deliver dispatch is sequenced strictly
after a green verify. The deliver dispatch carries the same task text
the implementation worked from.

## Gates: park, escalate, expire

`gates/` is one small abstraction used by every human wait point. A
`Gate` is a named slot a workflow parks on; the matching signal handler
calls `resolve(approved, payload)`, and the first signal wins. `wait()`
takes a deadline and an escalation point:

```python
outcome = await self._merge_gate.wait(
    timeout=timedelta(seconds=timeouts.merge_gate_s),
    escalate_after=timedelta(seconds=timeouts.merge_gate_s // 2),
    on_escalate=lambda: self.record_gate(input.run_id, "merge", "escalated", timeouts),
)
```

Nobody home? The escalation fires at half the deadline and the wait
continues; at the end the gate expires with a `timed_out` outcome. All
three transitions — escalated, verdict (approved/rejected), timed out —
are `source=process-gate` audit events, written by
the `record_gate_outcome` *activity*, because workflow code cannot reach
the registry. The park itself is API-visible through the `state` query
(`paused-merge_gate`, `paused-budget_exhausted`), so a waiting gate is
never a silent hang.

The merge gate is the only human gate the shipped blueprint enables, and
the sole owner of the merge guarantee. Merge approval is *workflow state*
— the single named exception to policy-side defence in depth — which is
why its outcomes are first-class audit rows and the merge itself is a
gateway-audited `git.merge_pr` call that policy grants to the platform
identity alone. Gate config comes from the compiled plan
(`workflows.<name>.gates`), never hardcoded.

The budget pause rides the same spine: when a dispatch returns the LLM
gateway's `budget_exhausted` marker, the workflow parks on the `budget`
signal rather than silently retrying against an exhausted cap. Approval
means the cap was raised — the same text is re-dispatched; anything else
finalizes the run with the budget error.

## Identity: bound deterministically, revoked by a row

Agent ids are deterministic from `{crew, role, run}` —
`ephemeral_agent_id()` is string concatenation, nothing more — so a bind
retried after a worker death lands on the *same* participants row,
instead of leaking a second identity. Nothing is signed: the agent's
credential is its own pod's projected ServiceAccount token, and the row —
which maps the SA name to run, crew coordinates, company and catalog
template — is what the gateways resolve on every call. Real revocation is
terminating that row. When a retried bind would revive a row the run's
teardown already swept, the registry raises `ParticipantRevoked` and the
caller abandons the identity — revocation is final, and a retry that lost
the race with teardown must not build on it.

`fulfill_crew` composes the whole fulfilment: consume the recorded crew
request, bind every agent and give each its own pod, hand back the agents
by crew role. Two details carry the design:

- **Fulfilment never improvises authority.** Without a recorded,
  unexpired crew request it refuses. When a workflow records one itself
  (`record_under_authority`), the recording is allowed only if the
  compiled decision the `crew.request` *tool* would face allows it: the
  activity evaluates the same bundle at OPA with the same enriched input
  (`may_spawn` narrowing, live spawn count, budget headroom).
- **Fulfilment provisions; it never dispatches.** Provisioning handles
  any number of agents, but every workflow dispatches exactly one, so a
  crew declaring more is refused before any pod exists. The workflow
  dispatches the agent with the task text itself — the orchestrator-fixed
  graph is the only handoff; there is no free-form crew chat.

Every bind, revoke, provision and destroy lands a `process-lifecycle`
audit event: a run's story shows who existed, when, under whose authority.

## Ephemeral pods

An ephemeral pod is one ephemeral agent's brain host: the same agent image a
standing agent runs, labeled to its run. Its only credential is the
audience-scoped projected token of its own per-run ServiceAccount,
mounted by the kubelet — no token-shaped env var exists, and no
credential ever enters Temporal's workflow history, which
is persisted and replayed. The manifest mirrors the operator's standing
posture: no API-server token, no service links, non-root, `GPG_KEY`
blanked.

The provisioner's Kubernetes surface is bounded by construction: it
creates and deletes pods and their labeled ServiceAccounts only, in the
company namespace only, and every
delete goes through the `{run_id, kind=ephemeral-agent}` label selector — so
nothing unlabeled, and no sandbox pod sharing the run label, can ever be
touched. Standing structure stays operator-owned.

Provisioning carries a race guard learned the hard way: removing a
same-name remnant of a crashed attempt can take ~30 seconds — ample time
for the run's teardown to sweep past a check made beforehand. So the
`still_wanted` predicate ("would anything still collect this pod?") is
evaluated *inside* `provision()`, immediately before pod creation, from
two independent signals: a live participants row and a non-terminal run
record. Refusal raises `EphemeralPodAbandoned` — not a failure of the run; the
run is already over. `activeDeadlineSeconds` is the backstop beneath the
guard: a pod created in the last instant before teardown passes has no
owner left to collect it, and the wall-clock deadline means such an
orphan stops burning compute within the hour.

## Teardown, the one compensation anchor

`teardown` runs on every terminal path — success, failure, cancel,
activity timeout, worker death (Temporal replays the workflow and drives
it there). In order: revoke the run's participants (the identity dies
first), destroy the ephemeral pods, destroy the sandbox, retire its gateway
registration, and write the honest terminal run record, which is also the
workflow's result. The participants sweep is deliberately independent of
the sandbox handle — binding precedes provisioning, so participants can
exist even when no sandbox ever did. Cleanup failures are folded into the
run record as errors rather than masking the outcome: a stale row is a
governance problem, never a silent one.

## Entry points

| What | How | Image |
|---|---|---|
| worker | `python -m orchestrator.worker` | `poseidon-orchestrator` |
| CLI | `uv run poseidon …` | run from a checkout |

```
uv run poseidon blueprint compile examples/hello-saas.yaml --out ./bundle
uv run poseidon blueprint preview examples/hello-saas.yaml
uv run poseidon blueprint apply examples/hello-saas.yaml --yes

uv run poseidon run examples/task-happy.md               # implement by default
uv run poseidon run --workflow intake examples/goal-injected-conductor.md
uv run poseidon review 41                                # review PR #41
uv run poseidon signal <run_id> merge --workflow review  # approve the gate
uv run poseidon signal <run_id> merge --no-approved --workflow review
uv run poseidon status <run_id>
```

The full command reference, and the Temporal workflow/signal/query
contract beneath it, live in [INTERFACES.md](../../INTERFACES.md). The
run-control commands hold no credentials — runs execute on the in-cluster
worker; the CLI only starts, signals and inspects, over `TEMPORAL_ADDRESS`
(default `127.0.0.1:7233`, i.e. a port-forward). `blueprint apply` is the
exception: it submits the Company and AgentInstance CRs with *your*
kubeconfig — cluster mutation stays with the operator, and the CLI's
authority there is exactly the caller's own.

## Credential posture

The worker holds the egress-proxy HMAC key
and the registry DSN — **no `GIT_TOKEN`, no provider keys, no signing
key** (there is nothing to sign: crew identities are ServiceAccounts,
bound by registry row). Its gateway
identity is an audience-scoped projected service-account token, distinct
from the automounted API-server token the k8s_jobs backend and the
crew-pod provisioner use: each is useless where the other is needed.

## Cross-package interactions

- [`gateway`](../mcp-gateway/README.md) for every governed call, including
  its own platform-lifecycle ones (`repo.archive`, sandbox registration,
  `sandbox.run_tests`, `git.read`, `git.merge_pr`).
- [`sandbox`](../sandbox/README.md) for the `SandboxBackend` contract and
  the k8s_jobs backend.
- [`registry`](../registry/README.md) for runs, participants, crew
  requests, companies, budgets and audit.
- [`compiler`](../compiler/README.md) + [`schema`](../schema/README.md)
  behind the `poseidon blueprint` commands.
- [`agent`](../agent/README.md) over HTTP for dispatch.

## Tests

```
uv run pytest packages/orchestrator
```

The unit suite covers activity behaviour and retry safety against fakes:
deterministic binding converging on one row, the crew-pod manifest's
containment posture, gate outcomes as audit events, budget-boundary
refusals, fail-closed config reads, the timeout profiles. Sequencing,
compensation and durability are workflow concerns, proven live by the
kind e2e suite — including killing the worker mid-run and asserting
Temporal replays it with nothing leaked.

## Invariants

- Workflows reference activities **by name**; the workflow modules import
  nothing from the activity implementations. That is the seam an engine
  swap would use.
- Workflow code cannot touch the registry (Temporal's sandbox), so every
  audit write goes through an activity.
- Teardown runs on success, failure, cancel, activity timeout and worker
  death.
- Fulfillment never improvises authority: without a recorded, unexpired
  crew request it refuses. When a workflow records one itself, it is
  allowed only if the compiled decision the `crew.request` *tool* would
  face allows it — same bundle, same enriched input.
- Ephemeral pod deletes always go through the `{run_id, kind=ephemeral-agent}`
  label selector, so nothing unlabeled — and no sandbox pod sharing the
  run label — can ever be touched.
- `activeDeadlineSeconds` on ephemeral pods: the teardown race guard refuses
  to provision for a finished run, but a pod created in the last instant
  before that becomes true has no owner left to collect it.
