# Pause, resume or delete a company

Pause the company when you want it to stop starting new work, for example
while you read its ledger or change its blueprint. Delete it when you are
done with it. Deleting is the only way to stop a company for good.

| You want to… | Run | What happens |
|---|---|---|
| Hold the company | `poseidon pause --reason "…"` | The heartbeat starts nothing new. Runs in flight finish. Everything stays readable. |
| Let it continue | `poseidon resume --reason "…"` | The heartbeat looks at the world at once and decides what to start. |
| Stop it for good | `poseidon company delete --reason "…"` | Running runs are cancelled, then the company is removed. Your repository and the audit records stay. |

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
bundle      023d5c831005ed5e
namespace   poseidon-company
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
uv run poseidon company delete --reason "wrong vision"
```

The command names the runs it will cancel and asks before it deletes.
Add `--yes` to skip the question.

```text
hello-saas: 1 running run(s) — they will be cancelled:
run             workflow    task
─────────────────────────────────────────────────
auto-f0fa3f44   implement   implement issue #2557
hello-saas deleted in 12 s; cancelled auto-f0fa3f44
```

Deleting cancels every running run, removes the company's agents and
policy, and waits for the teardown. Your repository, its pull requests and
issues, and the audit records stay. To bring the company back, apply the
same blueprint again.

## Next steps

- [Answer gates and follow a run](gates-and-runs.md)
- [How the heartbeat decides](../explanation/heartbeat.md)
