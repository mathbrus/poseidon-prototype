# Hand over the keys

Everything in [Getting started](getting-started.md) had you pressing
start and answering every merge. This guide grants the company those two
authorities, one at a time — and takes them back, since each is a diff.

## The heartbeat: the company starts its own runs

To let the company run its own backlog, apply a blueprint with a
`heartbeat` section — the delegated example carries one (and a delegated
merge gate; drop that block to keep every merge yours):

```bash
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

No command starts the heartbeat — that is the point. The orchestrator
worker notices that the applied blueprint now declares a `heartbeat` section
and starts one durable workflow, `heartbeat-hello-saas`, on its own. The
loop is event-triggered: every finished run nudges it awake, and
`reconcile_minutes` (30 in the example) bounds how long a change only the
outside world saw — an issue you file by hand, a PR you merge by hand —
waits for a look. Each tick:

1. **Observes** the open backlog, the delivered-but-unreviewed PRs (still
   open on the git host — one you merged by hand simply drops out), the
   in-flight runs and today's spend.
2. **Dispatches the PM's toolless *decide* procedure** for exactly one
   action — implement a ticket, review a PR, or idle — with a required
   one-line rationale, journaled as a `heartbeat.decide` audit event.
3. **Starts the chosen run** only if it wins the compiled `heartbeat.start`
   decision: within `max_concurrent_runs`, under the `daily_cap`, on an
   item fewer than `max_attempts` runs have taken up, and only a workflow
   on the `may_start` list. `intake` is deliberately not on that list —
   goals always enter through a human.

A run that dies before concluding anything (the model call timed out, the
reviewer hit its step cap) does not take its ticket or PR down with it:
the item is offered again until `max_attempts` runs have tried, and the
run that spends the last attempt leaves a comment on it saying so.

Your job shrinks to two acts: feed goals in
(`poseidon run --workflow intake …`) and answer merge gates. Watch it work:

```bash
uv run poseidon status hello-saas --workflow heartbeat   # the standing workflow
kubectl -n poseidon-platform logs -f deploy/orchestrator-worker
```

To take the keys back, apply a blueprint without the section —
`hello-saas.yaml` — and the heartbeat stops itself: each tick re-reads the
applied blueprint and exits when the section is gone, and the compiled
`heartbeat.start` rules vanish with it. `tests/e2e_kind/test_overnight.py`
runs this arrangement end to end with exactly two human acts: one goal in,
one approval out.

## The delegated gate: the company merges its own work

The one human act each run still needs is the merge approval. The flagship
profile delegates it — narrowly:

```bash
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

Three things change relative to a heartbeat-only company:

- `gates.merge.resolved_by` becomes `policy`, with a `delegate` block
  naming the path class the company may merge on its own
  (`slugify.py`, `tests/unit/`) and a rework bound (`max_reworks: 1`).
- A `policies.contract_tests` block declares the oracle: `tests/contract/`
  holds the acceptance spec delegated merges are graded by, and it can
  never be delegated.
- A new `contract-authoring` crew — the only role that may deliver into
  the contract tree — joins the PM's `may_spawn` list, so contract-first
  work can be sequenced.

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
    the acceptance suite's product (`slugify.py`). A Tetris PR from the
    getting-started walkthrough touches `tetris.py` — out of class, so it
    declines to your `merge` signal. Edit `delegate.paths` to name the paths
    you are willing to see merge on green.

With both keys handed over, the human surface is a single act:
`tests/e2e_kind/test_delegated.py` proves the arc — one goal in, and
the company scopes, implements, reviews and merges its own in-class
passing work, every decision on the audit record. Taking the merge back is
the same diff in reverse: re-apply a profile with `resolved_by: human` and
every merge parks on you again.
