# Delegate merges

By default, every pull request the company delivers waits for you to
approve the merge. This guide lets policy merge the company's own work in
the paths you choose, and shows how to take that back.

## Before you start

- A company applied from `examples/hello-saas.yaml`, as in the
  [Quickstart](../get-started/quickstart.md).
- Know what policy checks before it merges. [The merge
  gate](../explanation/merge-gate.md)
  explains it.

## What changes

Policy merges a pull request only when all of these hold:

- the independent reviewer passed it,
- the platform's own run of the tests is green,
- the reviewer is a different agent from the implementer,
- every changed path is in the class you delegate,
- the acceptance contract, if you declare one, is green.

Anything else still waits for you: a path outside the class, a change to
`.github/`, and any change to the contract tests. A failed review or a
red test run files a rework ticket instead, up to `max_reworks` times.

## Steps

### 1. Declare the delegated paths

In your blueprint, change the merge gate's resolver from `human` to
`policy` and list the paths policy may merge:

```diff
 workflows:
   review:
     crew: feature-review
     gates:
       merge:
-        resolved_by: human
+        resolved_by: policy
+        # The wait when policy declines and the merge falls back to you.
         deadline_minutes: 1440
+        delegate:
+          paths: [slugify.py, tests/unit/, docs/]
+          max_reworks: 1
```

`docs/` holds the company's strategy and architecture plan. If you
delegate it, the first strategy merges on the reviewer's verdict alone.
Leave `docs/` out if you want to read it yourself.

### 2. Optional: add tests the implementer can't rewrite

Delegating on "the tests pass" is only safe if the agent that writes the
code can't also weaken the tests. Declare a contract tree that a separate
crew writes and only you merge:

```yaml
policies:
  contract_tests:
    paths: [tests/contract/]
    roles: [contract-author]
```

Then let the PM start the crew that writes those tests:

```diff
 standing:
   pm:
     ...
-      crew: [feature-delivery, feature-review, stewardship]
+      crew: [contract-authoring, feature-delivery, feature-review, stewardship]
```

A delegated path can't overlap the contract tree, `.github/` or
`policies.deny_paths`. The compiler refuses the blueprint if it does.

`examples/hello-saas-delegated.yaml` is the shipped example with both
changes made.

### 3. Preview and apply

```bash
uv run poseidon blueprint preview company.yaml   # shows the gate change before it happens
uv run poseidon blueprint apply company.yaml --yes
```

## Check that it worked

Watch the next review run:

```bash
uv run poseidon follow
```

A delegated merge appears as a `merge.resolve` row allowed by the rule
`merge_resolve_delegated`, with no verdict from you. A pull request that
falls outside the class parks as before, in the state `parked-merge_gate`.

## Undo it

Delegation is a line in the blueprint, so revoking it is a diff. Set
`resolved_by` back to `human`, remove the `delegate` block, and apply
again. From the next review on, every merge waits for you.

## Next steps

- [Pause, resume or delete the company](pause-resume-delete.md)
- [The merge gate](../explanation/merge-gate.md):
  every fact policy checks, and what happens when one fails
- [Limitations](../explanation/limitations.md): what a delegated gate
  still can't catch
