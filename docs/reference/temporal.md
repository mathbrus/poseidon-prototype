# The contract beneath the CLI: Temporal

Every `poseidon` command is sugar over the Temporal client API. The CLI
confers no authority of its own — the real contract is the workflow
surface the orchestrator registers, and any process that can reach the
Temporal frontend can exercise all of it: start runs, deliver gate
verdicts, terminate workflows, read every run's history. A dashboard, a
chat-ops approval bot, or the e2e suite would integrate here, not by
shelling out to the CLI. This page writes that contract down; the tables
are rendered from the workflow classes at build time.

All workflows run on task queue `poseidon-orchestrator` and are named
deterministically: one workflow per run, `{workflow}-{run_id}`, and one
heartbeat per company, `heartbeat-{company}`.

## Workflows

<!-- gen: workflows -->

`implement` takes the number of a ticket the company filed; the platform
resolves the open issue first and composes the task from it. `scope`
takes nothing but a run id: its text is the observation the platform
composes. The heartbeat is started by the worker's ensure loop, never by a
human, and stops itself when the company is gone — and it is what starts
the three run-shaped workflows.

Activity bounds are not part of any input: the worker's `TIMEOUT_PROFILE`
(`default` for a paid brain, `fast` for the mock) sizes every workflow it
hosts, and each workflow learns it from its first config activity.

## Signals

Signals are how verdicts enter a parked workflow. The gate signals share
one signature, `(approved: bool, payload: str | None)`, which is exactly
what `poseidon signal` sends. The heartbeat's `wake` is the one exception:
it carries nothing at all, because it decides nothing — it ends the wait
to the next tick, which observes, decides and is authorized like any
other.

<!-- gen: signals -->

Under `resolved_by: policy` the compiled `merge.resolve` decision may
release the merge itself before the gate ever parks — but the `merge`
signal still resolves it, so the human override survives delegation.

## Queries

Every workflow exposes a `state` query returning the current phase as a
string — the mechanism behind the claim that a parked gate or a budget
stop is "an API-visible paused state, never a silent hang."

## What guards this surface

Nothing in the surface itself. The Temporal frontend has **no
authentication of its own**, and Poseidon's NetworkPolicies do not select
the Temporal pods — they are installed by Temporal's own chart. The
browsable `temporal-web` UI over this same authority is **opt-in**: the
kind bootstrap deploys it only with `TEMPORAL_WEB=1`. Two facts keep this
honest rather than alarming:

- **Agents cannot reach it.** Company namespaces are deny-all by
  construction, with holes only toward the two gateways and the egress
  proxy. The statement "agents have exactly one interface, the gateway"
  survives — by the operator's NetworkPolicy, not by Temporal's.
- **Humans reach it deliberately.** On kind there is no ingress; you hold
  operator authority exactly when you can run `kubectl port-forward` into
  the platform namespace — that is, when you already hold cluster
  credentials.

But state it plainly: *delivering a merge-gate verdict requires nothing
but network reach to port 7233.* On any cluster where the platform
namespace is reachable more broadly than intended, the merge gate is too.

One qualifier under a delegated gate: an in-class merge needs no signal at
all — the compiled `merge.resolve` decision releases it, and its authority
is exactly that decision (the caller-verified verdict, the platform's own
green checks, reviewer-independence, the delegated path class, and — when
the company declares a contract tree — `contract_green`, the base-ref
contract tree run against the PR head), nothing more. Every one of those is
a platform-computed value passed as a real argument, so adversarial prose in
the diff cannot rewrite it; a company with no `contract_tests` declared
never has the contract fact read. Frontend reach still matters for the human
override and for every class the delegation does not cover, which stay
signal-resolved.
