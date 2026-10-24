# The merge gate

The merge gate decides whether a reviewed pull request merges. By
default you resolve it. With an `automate` block, policy may merge the
paths you choose without you: an automated merge. This page explains
both, and why your verdict counts only once it is recorded.

Without a `gates.merge` section, a review ends without merging anything.
No company merges automatically by default.

## When you resolve it

```yaml
workflows:
  review:
    gates:
      merge:
        deadline_minutes: 1440
```

After the review, the run parks in the state `parked-merge_gate` and waits
for you. You answer with `poseidon verdict` ([how](../how-to/gates-and-runs.md)).

The order of what follows is the point:

1. The console verifies who you are.
2. It asks policy whether `company.humans` names you.
3. It records your verdict and your reason in the audit table, under your
   name.
4. Only then does it signal the waiting workflow.
5. The workflow treats the signal as a prompt to look. It reads your
   recorded verdict back from the audit table, and policy releases the
   merge only on that record (rule `merge_resolve_human`).

A signal that reached the workflow any other way, for example through a
port-forward to Temporal, finds no recorded verdict. The run ends
unmerged, and the refusal is recorded as `gate_verdict_unverified`.

If nobody answers, the gate escalates at half of `deadline_minutes` and
expires at the end. Both events are recorded.

## When the merge is automated

```yaml
workflows:
  review:
    gates:
      merge:
        deadline_minutes: 1440
        automate:
          paths: [slugify.py, tests/unit/, docs/]
          max_reworks: 1
```

Policy merges with no human act, but only when every one of these facts
holds. The platform establishes each one itself. None comes from an
agent's word.

- The platform's own run of the tests on the pull request is green. It's
  the same test code that gates delivery.
- The independent reviewer's verdict passed.
- The registry confirms the reviewer is a different agent from the
  implementer.
- Every changed path is inside `automate.paths`.
- If the company declares a contract tree, the contract is green (see
  below).

The checks run in this order:

1. **The platform runs the tests** in the sandbox built from the pull
   request, before the reviewer starts. The reviewer holds a shell in that
   sandbox, so a run after it could read whatever the reviewer left there.
   If the tests are red, the rework ticket is filed at once and no
   reviewer is started.
2. **The reviewer** reads the diff, runs the tests in its sandbox, posts
   findings and gives its verdict.
3. **The contract run**, if the company declares a contract tree, uses a
   new sandbox (see below).
4. **Policy decides** on the facts.

Anything short of that is never a quiet merge:

| The pull request | What happens |
|---|---|
| Every fact holds, every path automated | Policy merges it (rule `merge_resolve_automated`, gate outcome `automated`). |
| Touches `.github/` or a path not automated | Automation declines (gate outcome `automation_declined`), and it parks on you as if the gate had no `automate` block. |
| Touches the contract tree | It parks on you at once, before any check runs. Only a person merges the tests that grade the code. |
| The verdict failed, the tests are red, or the contract is red | The findings become a rework ticket, and the platform closes the pull request. After `max_reworks` rounds, it parks on you, with the reviewer's findings on it. |

`automate.paths` can never include `.github/`, a path in
`policies.deny_paths`, or the contract tree. The compiler refuses such a
blueprint, because a merged pull request must never be able to rewrite
the rules or the tests that govern merging.

## Tests the implementer can't rewrite

If "the tests pass" is what lets policy merge, the agent that writes the
code must not also own the tests that grade it. A blueprint can split
them:

```yaml
policies:
  contract_tests:
    paths: [tests/contract/]
    roles: [contract-author]
```

- **The contract tree** (`tests/contract/`) is the oracle. Only the
  `contract-author` role may open a pull request into it. When the
  implementer tries, the gateway denies the call with the rule
  `git_open_pr_contract_tree`.
- **Everything else under `tests/`** stays the implementer's.

The PM orders the work by filing two tickets to two crews: first the
contract, then the code.

**The contract fact.** To check a pull request, the platform takes the
code from the pull request and the contract tree from the *base branch*.
It runs those tests itself. Nothing in the contract came from the pull
request, so weakening a test, adding a skip marker or breaking collection
in the pull request changes nothing: the code meets the contract you
already approved, or it doesn't merge.

Until a contract tree exists on the base branch, the fact holds trivially.
The first contract pull request always needs you, like every contract
change. On any pull request that edits the contract tree, the platform
also posts how many test functions and assertions it removes, so a gutted
spec can't be approved without anyone noticing.

## The merge is the reviewed commit

A review reads the pull request's head commit once, when it starts. Every
sandbox the review uses is built from that commit, and the merge names
it. If anyone pushes to the branch after that, GitHub refuses the merge:
the run fails with `head moved after review`, and the pull request stays
open for another review. This holds whoever resolves the gate.

## After the merge

Whoever resolved the gate, the orchestrator performs the merge under its
own identity. No agent holds the merge tool. Right after, the platform
closes the tickets the pull request fulfilled. For a rework chain, that
is every ticket in the chain.

## What it doesn't guarantee

An automated gate merges code that meets the contract but is wrong in ways
the contract doesn't test. The record your verdict lives in is an
unsigned audit row. [Limitations](limitations.md) lists these edges in
full.
