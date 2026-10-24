# orchestrator

The orchestrator is the Temporal worker that runs the company's four
workflows: `heartbeat`, `scope`, `implement` and `review`. It runs as
one Deployment in the platform namespace and starts the company's
heartbeat itself. The heartbeat starts every other run, and the console
can also start, signal or cancel one for you.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-orchestrator` | `python -m orchestrator.worker` | `EGRESS_PROXY_KEY`, `REGISTRY_DSN` | `runs`, `run_participants`, `audit_events` (`process-gate`, `process-lifecycle`); ephemeral pods and their ServiceAccounts |

## Module map

| Path | Owns |
|---|---|
| `workflows/` | The four workflows. `base.py` holds `RunWorkflow` (the run's birth, crew fulfillment, the budget park, teardown), the retry policies and the timeout profiles. |
| `activities/` | Every side effect. `run.py` is the lifecycle every run shares. `implement.py`, `review.py`, `crew.py`, `heartbeat.py`, `observation.py`, `bootstrap.py`, `documents.py` and `tickets.py` serve their workflows. `blueprint.py` and `company.py` read the applied spec. |
| `gates/` | `Gate`, a signal-driven wait point with an escalation timer, and `GateOutcome`. |
| `ephemeral_pods.py` | `EphemeralPodProvisioner`: the bounded Kubernetes surface for ephemeral pods. |
| `identity.py` | `ephemeral_agent_id()`, the deterministic `{crew}-{role}-{run_id}` name. |
| `audit.py` | `record_event()`, the platform's own audit rows. |
| `clients.py` | `GatewayClient` (MCP tools and admin routes) and `AgentClient` (dispatch and poll). |
| `config.py` | `WorkerSettings`, every field set by the chart. |
| `worker/` | The worker process: registers the workflows and activities on the `poseidon-orchestrator` task queue, and every 60 seconds starts `heartbeat-{company}` if it isn't running. |

## How it works

### Workflow code is deterministic

Temporal replays a workflow function from the top after a worker dies,
substituting recorded activity results. So workflow code may only
sequence activities, park on signals and compose text from values in
history (`verify_feedback`, `review_task`). It never reads the
environment, does I/O or touches the registry. Everything else is an
activity.

- Workflows call activities by name. The workflow modules import
  nothing from `activities/`.
- Each activity module's docstring states why each activity is safe to
  retry. A new activity needs that line. Non-idempotent activities whose
  caller re-observes anyway (`heartbeat_bootstrap`, `wake_heartbeat`)
  use `SINGLE_ATTEMPT`.
- Timeouts are per activity, never per workflow, because a workflow
  timeout would skip compensation. Their sizes come from the worker's
  `TIMEOUT_PROFILE` (`default` or `fast`). Each workflow learns it from
  `workflow_config`. The registry round trips before that use
  `BOOTSTRAP_S`.

### Each workflow is a fixed sequence of activities

| Workflow | Sequence |
|---|---|
| `heartbeat` | `heartbeat_config` → `heartbeat_observe` → [`heartbeat_bootstrap`] → `heartbeat_decide` → `heartbeat_authorize` → start a child run (abandoned) → `record_tick` → wait for a `wake` signal or `reconcile_minutes` → continue-as-new |
| `scope` | birth → `scope_observation` → dispatch the conductor's `scope` procedure → teardown |
| `implement` | birth → `read_issue` → `fetch_workspace` → `provision_sandbox` → `fulfill_crew` → `dispatch_documents` → `implement` / `platform_verify` loop → `deliver` → teardown |
| `review` | birth → `read_pr` → `fetch_workspace` → `provision_sandbox` → `fulfill_crew` → `review` → `record_verdict` → merge gate → `merge_pr` → `close_tickets` → teardown |

Birth is `create_run` → `check_budget` → `workflow_config`. A run born
without budget headroom fails before any dispatch. Every dispatch
carries the bounds `workflow_config` rendered. After teardown, a run
that concluded nothing calls `record_attempts_exhausted`. Every run then
wakes the heartbeat.

The implementer's completion claim is advisory. `platform_verify`, the
worker's own test run in the sandbox, is the gate of record. A red
verify re-dispatches with the failing output, up to
`max_verify_retries`. Only a green verify reaches the separate `deliver`
dispatch. The `implement` procedure's toolset has no `open_pr`.

On the merge gate's human path, an approving `merge` signal is not the
release. `read_human_verdict` reads the verdict the console recorded,
and `resolve_merge` evaluates `merge.resolve` on it. With no recorded
verdict, the run ends unmerged with a `gate_verdict_unverified` event.
On the automated path, a failed layer files a rework ticket and closes
the pull request. At `max_reworks`, or when automation declines, the run
parks on the human. See [The merge gate](../../docs/explanation/merge-gate.md)
and [The heartbeat](../../docs/explanation/heartbeat.md).

### Waits are gates, and teardown always runs

A `Gate` keeps the first signal. It escalates at
half its deadline and expires at the end. Each transition is a
`process-gate` row written by `record_gate_outcome`, which refuses any
kind outside `common.workflows.GATE_OUTCOMES`. The park itself is the
first (`gate_parked`), written before the `state` query shows
`parked-<gate>`, so a run that reads parked always has the row its
deadlines are measured from. The budget park lives in `RunWorkflow.dispatch`. A
`budget_exhausted` result parks on the `budget` signal. An approving
verdict re-dispatches under the cap as currently applied.

`fulfill_crew` evaluates `crew.request` at OPA as the conductor, with
the live spawn count and budget headroom. It audits the result as
`lifecycle.record_request` and binds nothing on a deny. It refuses a
crew that doesn't declare exactly one agent. Each agent gets a
`run_participants` row and its own pod. A retried bind lands on the same
row. A bind after teardown raises `ParticipantRevoked`, and the caller
abandons it.

`EphemeralPodProvisioner` creates and deletes only pods and their
ServiceAccounts, only in the company namespace. Every delete goes
through the
`poseidon.dev/kind=ephemeral-agent,poseidon.dev/run-id=<run>` selector.
Removing a crashed attempt's pod waits for it to terminate, and teardown
can pass meanwhile. So `provision()` checks `still_wanted` (a live
`run_participants` row and a non-terminal run) right before creating the
pod. A refusal raises `EphemeralPodAbandoned`. `activeDeadlineSeconds`
(one hour) stops any pod that slips past.

`teardown` runs on every terminal path, worker death included. Its order
is fixed: revoke the run's ephemeral agents, destroy ephemeral pods,
destroy the sandbox, unregister it at the gateway, link filed tickets,
then write the terminal run record. A failed step never skips
the steps after it. Most failures become the run's error when it has
none yet.

## Invariants

- A retried bind converges on one identity:
  `test_crew_activities.py::test_retried_bind_converges_on_one_identity`.
- A bind that lost the race with teardown is refused:
  `test_a_bind_that_lost_the_race_with_teardown_is_refused`.
- Fulfillment wins `crew.request` as the conductor, and a deny binds
  nothing: the decision tests in `test_ephemeral_pods.py`.
- A crew of more than one agent is refused:
  `test_fulfill_refuses_a_crew_with_more_than_one_agent`.
- A run torn down mid-provision gets no pod:
  `test_a_run_torn_down_mid_provision_gets_no_pod`.
- Ephemeral pods carry a wall-clock deadline:
  `test_ephemeral_pod_carries_a_wall_clock_deadline`.
- Teardown revokes the run's ephemeral agents on every path:
  `test_run_activities.py::test_teardown_revokes_participants_on_every_terminal_path`.
- A gate outcome outside `GATE_OUTCOMES` is never written:
  `test_unknown_gate_outcome_is_refused_before_it_is_written`.
- A run born exhausted fails before dispatch:
  `test_budget_boundary_fails_born_exhausted_runs`.
- A signal without a recorded verdict merges nothing:
  `test_review_workflow.py::test_a_signal_without_a_recorded_verdict_merges_nothing`.
- A paused tick starts nothing:
  `test_heartbeat_workflow.py::test_a_paused_tick_looks_at_nothing_and_a_resumed_one_starts_work`.
- Deleting the company stops its heartbeat:
  `test_deleting_the_company_stops_a_running_loop`.
- The fast profile is tighter on every bound:
  `test_workflow_timeouts.py::test_fast_profile_is_strictly_tighter_on_every_bound`.

## Testing

```bash
uv run pytest packages/orchestrator
```

Activity tests run against the doubles in `registry.memory` and a fake
gateway. Workflow tests run in Temporal's time-skipping environment,
which downloads its test server on first use and skips if it can't. Run
tests per package, as
[Contributing](../../docs/developing/contributing.md) explains.

The [acceptance suite](../../docs/developing/acceptance.md) proves what
needs a cluster.
`test_crew_fulfillment.py::test_worker_kill_leaks_no_crew` kills the
worker mid-run. `test_ephemeral_pods.py` checks the worker's RBAC and
the label-bounded destroy. `test_verify_gate.py` and
`test_review_gate.py` drive the two gates end to end.

## Related

- [Temporal](../../docs/concepts/temporal.md), and
  [Workflows](../../docs/reference/workflows.md) for the workflow,
  signal and query contract.
- [Standing and ephemeral
  agents](../../docs/concepts/standing-vs-ephemeral.md), [Security
  model](../../docs/explanation/security-model.md).
- [`console`](../console/README.md) starts and signals these workflows.
  [`agent-host`](../agent-host/README.md) runs the dispatched
  procedures.
- [`mcp-gateway`](../mcp-gateway/README.md) carries every governed call.
  [`sandbox`](../sandbox/README.md) provides the `k8s_jobs` backend.
  [`registry`](../registry/README.md) holds the rows.
- [`reconciler`](../reconciler/README.md) creates the Role this worker's
  Kubernetes access is bounded by.
