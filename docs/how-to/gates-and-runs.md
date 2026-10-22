# Answer gates and follow a run

The company runs on its own, but some steps wait for you. A run *parks* on
a gate until you answer it. This guide shows how to watch the company, find
a parked run, answer it, and read back what happened.

## Watch the company

```bash
uv run poseidon follow
```

`follow` prints every audit row as it lands, one line each: heartbeat
ticks, tool calls, decisions, gates and verdicts. It prints the spend each
time it moves. Press Ctrl+C to stop.

```text
19:42:08  heartbeat                     platform/orchestrator       heartbeat.decide  allow (decision_valid)
          ticket #2556 is the oldest open work and nothing is waiting on review.
19:42:09  heartbeat                     platform/orchestrator       heartbeat.tick  allow (tick_started:implement_2556_as_auto-c8357daa)
…
19:42:54  auto-c8357daa                 steward/stewardship-steward-auto-c8357daa  git.open_pr  allow
          The branch carries the task's change with its tests; the suite ran green in the sandbox, so the work is complete and delivered.
19:43:05  heartbeat                     platform/orchestrator       heartbeat.decide  allow (decision_valid)
          PR #2558 is delivered and unreviewed; a review unblocks the merge gate before new work starts.
```

To watch one run only, add `--run <run_id>`.

## Find a parked run

`runs` lists the company's runs, the running ones first:

```bash
uv run poseidon runs
```

```text
run             workflow    task                        status    started     took
──────────────────────────────────────────────────────────────────────────────────
auto-3d73e674   review      review pull request #2558   running   0 min ago   53 s
auto-c8357daa   implement   implement issue #2556       done      1 min ago   54 s
```

`run show` tells you whether a running run waits for you. Its
`execution` line ends with the run's state:

```bash
uv run poseidon run show <run_id>
```

```text
run        auto-3d73e674
workflow   review
task       review pull request #2558
status     running
started    0 min ago
took       53 s
verdict    pass
execution  workflow RUNNING review-auto-3d73e674 (parked-merge_gate)
```

A run waiting for you is in one of two states:

| State | It waits for |
|---|---|
| `parked-merge_gate` | Your merge verdict on the pull request it reviewed. |
| `parked-budget_exhausted` | Your verdict to continue past `budgets.per_run.usd`. |

A gate doesn't wait forever. It escalates at half its `deadline_minutes`
and expires at the end, and both events are recorded.

## Answer the gate

Read the pull request on GitHub, then give your verdict and your reason:

```bash
uv run poseidon verdict <run_id> merge approve --reason "engine first, as the vision asks"
```

```text
verdict recorded: merge approved for run auto-3d73e674, by owner
```

| To… | Run |
|---|---|
| Merge the pull request | `poseidon verdict <run_id> merge approve --reason "…"` |
| Refuse the merge | `poseidon verdict <run_id> merge reject --reason "…"` |
| Let a run over budget continue | `poseidon verdict <run_id> budget approve --reason "…"` |
| Stop a run over budget | `poseidon verdict <run_id> budget reject --reason "…"` |

The console records your verdict under your name before it tells the run.
The merge only goes ahead if that record exists, so a verdict from anyone
the blueprint doesn't name in `company.humans` is refused.

## Read what happened

| To see… | Run |
|---|---|
| One run's record | `poseidon run show <run_id>` |
| Everything a run did, from every component | `poseidon run story <run_id>` |
| Only what was decided, and why | `poseidon run story <run_id> --decisions` |
| The heartbeat's ticks | `poseidon heartbeat show` |
| The company's spend, or one run's | `poseidon spend`, `poseidon spend <run_id>` |

For example, the decisions of a review run you approved: the reviewer's
verdict, yours, and the ticket the merge closed.

```bash
uv run poseidon run story auto-3d73e674 --decisions
```

```text
19:43:47  auto-3d73e674                 platform/orchestrator       review.verdict  allow
          Suite green in the review sandbox; change matches the acceptance criteria.
19:44:16  auto-3d73e674                 human/owner                 gate.resolve  allow
          the strategy reads well
19:44:21  auto-3d73e674                 platform/orchestrator       issues.close  allow
          merged pull request #2558 (223c09270bd5ddaabbb3e2398ee1427d8b7a48cf): Write the strategy
```

Every read command takes `--json` and prints the console's answer as
stored.

## Start or cancel a run by hand

The heartbeat starts the company's runs. You can start one yourself, for
example to retry a ticket:

```bash
uv run poseidon start implement 42                 # implement ticket #42 and follow it
uv run poseidon start review 43 --no-wait          # review pull request #43 and return
uv run poseidon run cancel <run_id> --reason "…"   # stop a run
```

## Next steps

- [Delegate merges](delegate-merges.md), so in-scope work merges without you
- [Pause, resume or delete the company](pause-resume-delete.md)
- [CLI reference](../reference/cli.md)
