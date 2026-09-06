# Hand over the keys

Everything in [Getting started](getting-started.md) had you answering
every merge. The company already starts its own runs — every company has
a heartbeat, and stopping it is deleting the company — so the one
authority left to grant is the merge, and this guide grants it, narrowly,
and takes it back, since it is a diff.

## What the heartbeat already does

No command starts the heartbeat. The orchestrator worker keeps one durable
workflow, `heartbeat-hello-saas`, running for as long as the company is
applied. The loop is event-triggered: every finished run nudges it awake,
and `reconcile_minutes` (30 in the example) bounds how long a change only
the outside world saw — an issue you file by hand, a PR you merge by hand —
waits for a look. Each tick:

1. **Observes** the company's documents at the base ref, the open backlog,
   the delivered-but-unreviewed PRs (still open on the git host — one you
   merged by hand simply drops out), the in-flight runs and today's spend.
2. **Files bootstrap tickets** for a document still absent, and otherwise
   **dispatches the PM's toolless *decide* procedure** for exactly one
   action — scope the next slice, implement a ticket, review a PR, or idle
   — with a required one-line rationale, journaled as a `heartbeat.decide`
   audit event.
3. **Starts the chosen run** only if it wins the compiled `heartbeat.start`
   decision: within `max_concurrent_runs`, under the `daily_cap`, on an
   item fewer than `max_attempts` runs have taken up, only a workflow on
   the `may_start` list, and — for scoping — under `max_open_tickets`.

A run that dies before concluding anything (the model call timed out, the
reviewer hit its step cap) does not take its ticket or PR down with it:
the item is offered again until `max_attempts` runs have tried, and the
run that spends the last attempt leaves a comment on it saying so. A
bootstrap ticket that exhausts its attempts halts the loop by name
(`heartbeat_bootstrap_exhausted`, a process-gate event) instead of scoping without
a strategy; close the ticket and the loop refiles.

Your job is two acts: keep the charter (edit the blueprint, re-apply) and
answer merge gates. Watch it work:

```bash
uv run poseidon heartbeat state                          # the standing workflow and its ticks
kubectl -n poseidon-platform logs -f deploy/orchestrator-worker
```

Narrowing `heartbeat.may_start` keeps a step for an operator: a company
that may not `implement` on its own waits for `poseidon run <number>`.
Removing the section is not a state — it does not compile. To stop the
company, delete it: the loop reads no applied blueprint at its next tick
and stops. `tests/e2e_kind/test_overnight.py` runs the whole arrangement
end to end with exactly one human act: the approval out, through the
console.

## The delegated gate: the company merges its own work

The one human act each run still needs is the merge approval. The flagship
profile delegates it — narrowly:

```bash
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

Three things change relative to the human-gated company:

- `gates.merge.resolved_by` becomes `policy`, with a `delegate` block
  naming the path class the company may merge on its own
  (`slugify.py`, `tests/unit/`, and `docs/` — the company's own documents)
  and a rework bound (`max_reworks: 1`).
- A `policies.contract_tests` block declares the oracle: `tests/contract/`
  holds the acceptance spec delegated merges are graded by, and it can
  never be delegated.
- A `contract-authoring` crew — the only role that may deliver into the
  contract tree — joins the PM's `may_spawn` list, so contract-first work
  can be sequenced.

A review run of an in-class PR does not park. When the independent
verdict passed, the platform's own re-run of the tests and the contract
baseline are green, and every changed path is in class, the orchestrator
merges with no human command —
[the merge gate](../argument/anatomy.md#the-merge-gate-human-or-policy)
lists the facts and the fallbacks. Everything else still stops for you: an
out-of-class path, a carve-out surface, and any edit to the contract tree
park on the same `merge` signal you answered before, and a failed quality
layer feeds the bounded rework loop instead.

!!! note
    The delegated class is yours to declare, and the example's is scoped to
    the acceptance suite's product (`slugify.py`) plus the company's own
    documents. Delegating `docs/` means the first strategy — the company's
    reading of your vision — merges on the reviewer's verdict alone; leave
    it out of `delegate.paths` to read that reading yourself.
    [What it cannot do](../argument/cannot.md) names the residual.

With the merge handed over, no human act remains. The human surface —
the console — still stands, idle; every approval you did give is on the
record under your name. `tests/e2e_kind/test_delegated.py` proves the arc
— the company scopes, implements, reviews and merges its own in-class
passing work, every decision on the audit record, and nobody touches
anything. Taking the
merge back is the same diff in reverse: re-apply a profile with
`resolved_by: human` and every merge parks on you again.
