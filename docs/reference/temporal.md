# The contract beneath the console: Temporal

Every act a person performs through [the console](console.md) ends as a
Temporal client call: a workflow started, a signal delivered, an
execution described. The console confers the authority — it verifies the
person, wins the compiled decision, records the act — and Temporal
carries it out; the real contract is the workflow surface the
orchestrator registers, and this page writes it down. The tables are
rendered from the workflow classes at build time. Nothing integrates
here directly: a web UI, a chat-ops bot or the acceptance suite is a
client of the console, and the raw surface below it is fenced.

All workflows run on task queue `poseidon-orchestrator` and are named
deterministically: one workflow per run, `{workflow}-{run_id}`, and one
heartbeat per company, `heartbeat-{company}`. Both names live in
`common.workflows`, shared by the worker that hosts them and the console
that addresses them.

## Workflows

<!-- gen: workflows -->

`implement` takes the number of a ticket the company filed; the platform
resolves the open issue first and composes the task from it. `scope`
takes nothing but a run id: its text is the observation the platform
composes, and no human starts it. The heartbeat is started by the worker's
ensure loop, never by a human, and stops itself when the company is gone —
and it is what starts the three run-shaped workflows.

Activity timeouts are not part of any input: the worker's `TIMEOUT_PROFILE`
(`default` for a paid brain, `fast` for the mock) sizes every workflow it
hosts, and each workflow learns it from its first config activity.

## Signals

Signals are how verdicts reach a parked workflow — and a signal is a
nudge to look, never the verdict itself. The gate signals share one
signature, `(approved: bool)`, which is what the console sends after it
has recorded the verdict under the person's name. The heartbeat's `wake`
carries nothing at all, because it decides nothing: it ends the wait to
the next tick, which observes, decides and is authorized like any other.
The pause is not a signal: the console records it in the registry and
the loop reads it on every tick (and again at authorize time), so the
`wake` a pause or a resume sends only makes the loop look now.

<!-- gen: signals -->

The merge gate's release is not the `merge` signal. After the gate
approves, the review workflow reads the console's recorded verdict back
from the audit store (`read_human_verdict`) and the compiled
`merge.resolve` decision releases the merge on its human path —
`merge_resolve_human`, a recorded approving verdict by one of the
company's humans — or refuses by name: `merge_resolve_no_human_verdict` when no
verdict was recorded, `merge_resolve_human_rejected` when the latest one
rejects, `merge_resolve_not_a_human` when its author is not in
`company.humans`. A refusal records `gate_verdict_unverified` and ends the
run failed, unmerged. Under `resolved_by: policy` the delegated path may
release the merge before the gate ever parks; a decline still falls to the
human path, verified the same way, so the override survives delegation.

The budget gate is decided and recorded by the console like the merge
gate, but the workflow does not read its record back: a forged budget
verdict only re-dispatches under the per-run cap the LLM gateway
enforces from the ledger, and cannot spend past the stop.

## Queries

Every workflow exposes a `state` query returning the current state as a
string — the mechanism behind the claim that a parked gate or a budget
stop is "an API-visible parked state, never a silent hang." The console
surfaces it when a run is read, and requires the parked state before it
records a verdict on that gate.

## What guards this surface

The Temporal frontend has no authentication of its own, and two things
stand in for it.

- **The fence.** A NetworkPolicy in the platform namespace admits exactly
  the components whose job the frontend is — the orchestrator worker,
  which hosts the workflows, and the console, which starts, signals and
  inspects them on a person's behalf — plus Temporal's own pods. Every
  other platform pod, and every company pod, is refused at the network.
  The browsable `temporal-web` UI is one more pod inside the fence, and
  opt-in: the kind bootstrap deploys it only with `TEMPORAL_WEB=1`.
- **The verdict.** A human with cluster credentials still reaches the
  frontend by `kubectl port-forward`, which enters the pod's own network
  namespace beneath any policy. State it plainly: *network reach to port
  7233 delivers a signal.* But a signal without a console-recorded verdict
  fails the run — the compiled `merge.resolve` decision reads the record,
  not the signal — so the road that stays open releases nothing.
  `tests/e2e_kind/test_console_redteam.py` walks it on every acceptance
  run. A `wake` by that road buys one tick, authorized like any other;
  it cannot pause or resume the loop, which reads the registry's record.

Under a delegated gate an in-class merge needs no signal at all — the
compiled decision releases it, and its authority is exactly that decision
(the caller-verified verdict, the platform's own green checks,
reviewer-independence, the delegated path class, and — when the company
declares a contract tree — `contract_green`, the base-ref contract tree
run against the PR head), nothing more. Every one of those is a
platform-computed value passed as a real argument, so adversarial prose
in the diff cannot rewrite it; a company with no `contract_tests`
declared never has the contract fact read.
