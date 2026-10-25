# Pause, resume or delete a company

Pause the company when you want it to stop starting new work, for example
while you read its ledger or change its blueprint. Delete it when you are
done with it. Deleting is the only way to stop a company for good, and it
erases everything the platform recorded about the company.

| You want to… | Run | What happens |
|---|---|---|
| Hold the company | `poseidon pause --reason "…"` | The heartbeat starts nothing new. Runs in flight finish. Everything stays readable. |
| Let it continue | `poseidon resume --reason "…"` | The heartbeat looks at the world at once and decides what to start. |
| Stop it for good | `poseidon company delete --reason "…"` | Running runs are cancelled, then the company is removed and its records erased. Your repository stays as it is. |
| Stop it and reset its repository | `poseidon company delete --reason "…" --purge-remote` | The same, and the repository's open work is closed and its branches reset. |

Each of these is recorded under your name with your reason. You need to be
named in the blueprint's `company.humans`.

## Pause the company

```bash
uv run poseidon pause --reason "reading the ledger with nothing moving"
```

```text
heartbeat paused by owner
```

While the company is paused:

- Each heartbeat tick records `tick_paused` with your name and starts nothing.
- Runs that were already running finish normally.
- You can still start a run by hand, answer a gate or cancel a run.
- Applying the blueprint again does not lift the pause.

Check who holds it:

```bash
uv run poseidon company show
```

```text
company     hello-saas
phase       converged
generation  1 (observed 1)
bundle      78a4560686ef3cd4
repository  github.com/mathbrus/poseidon-poc-test-repository
heartbeat   paused by owner, just now
```

## Resume it

```bash
uv run poseidon resume --reason "read it"
```

```text
heartbeat resumed
```

## Delete the company

```bash
uv run poseidon company delete --reason "wrong vision" --yes
```

Without `--yes`, the command names the runs it will cancel and asks
before it deletes.

```text
hello-saas: 0 running run(s)
hello-saas deleted in 34 s
```

Deleting runs these steps, and the command returns once they are all
done:

1. It holds the heartbeat, so no tick starts a run while the company goes.
2. It cancels every running run and waits for each to tear down its
   agents and sandbox.
3. It removes the company's agents, policy and namespace.
4. It erases everything the platform recorded about the company: its
   runs, tickets, spend and audit records, your delete's own included, and
   its workflow histories.

The command's answer is your receipt: nothing else of the company is left
to read. What stays is the platform's own. The company's policy stays
revoked, and records that name no company stay, such as a token the
console refused.

Applying the same blueprint again starts a new company with no history.
Its daily cap starts again at $0 for the day. Only a person the blueprint
names in `company.humans` can delete the company and apply it again.

The whole delete may take at most 600 seconds. If a step can't finish
in time, the command fails and names the step. What the delete already
did stands: the company is still applied, held, and its runs are ended.
Run the delete again to finish it.

### Purge the repository too

```bash
uv run poseidon company delete --reason "start over" --purge-remote
```

`--purge-remote` also resets the company's repository in place, after
the runs end and before the company goes:

- Every open pull request is closed.
- Every open issue is closed as not planned.
- Every branch is deleted except the base branch and the repository's
  default branch.
- Those two branches are forced to one new commit with no files.

The command names the repository and asks you to type its `owner/name`.
`--yes` skips that question too.

```text
hello-saas: 0 running run(s)
--purge-remote resets github.com/mathbrus/poseidon-poc-test-repository in place: every open pull request and issue closed, every branch but the base and default ones deleted, and those forced to one empty root commit. Closed issues and pull requests stay visible on the git host.
type mathbrus/poseidon-poc-test-repository to purge it: mathbrus/poseidon-poc-test-repository
hello-saas deleted in 37 s; its repository purged
```

Closed pull requests and issues stay visible on GitHub: its API can't
delete pull requests, and deleting issues needs admin rights. The
platform's GitHub token needs no new permission, but the base branch must
allow force pushes. If it is protected, GitHub refuses the reset before
anything else changes. The delete then fails, and the company is still
applied and held. Allow force pushes on the branch, or delete without
`--purge-remote`.

Only the console can purge, under its own identity at the gateway, and
only on the company's repository. No agent can be granted it.

## Next steps

- [Answer gates and follow a run](gates-and-runs.md)
- [How the heartbeat decides](../explanation/heartbeat.md)
