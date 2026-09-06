# The `poseidon` CLI

The CLI is a **thin Temporal client**. It connects to the Temporal
frontend (`TEMPORAL_ADDRESS`, default `127.0.0.1:7233` — on kind, reach it
with `kubectl -n poseidon-platform port-forward svc/temporal-frontend
7233:7233`) and starts, signals, or inspects workflows. The run-control
commands (`run`, `review`, `signal`, `status`) hold no credentials and
perform no work themselves: every run executes on the in-cluster worker,
under the platform's identity, regardless of who invoked the CLI or from
where. The `blueprint` commands are the exception — `apply` submits custom
resources with your kubeconfig, and `--live` compiles read the registry.

Workflow ids are `{workflow}-{run_id}` across the three run-shaped
workflows (`scope`, `implement`, `review`), which is why several commands
take a `--workflow` option: the CLI needs to know which workflow a run
belongs to in order to address it. The fourth workflow is not run-shaped:
the heartbeat is one standing workflow per company, `heartbeat-{company}`,
started by the worker's ensure loop for as long as the company is applied
(`poseidon status <company> --workflow heartbeat` inspects it). No human
starts it, and no human starts the runs it starts — its authority is
exactly the compiled `heartbeat.start` decision, nothing more.

The human's acts are the charter and the gates:

```bash
uv run poseidon blueprint apply company.yaml --yes       # the charter: vision, caps, gates
uv run poseidon signal <run_id> merge --workflow review  # approve a parked merge
uv run poseidon signal <run_id> merge --no-approved --workflow review   # reject it
uv run poseidon signal <run_id> budget --approved        # raise a cap the run stopped at
```

`run` and `review` start one run by hand — an operator reproducing a run
on a ticket the company filed or a pull request it delivered. They are not
how work enters: nothing does, except the company's own documents.

```bash
uv run poseidon run 42                                   # implement ticket #42, by hand
uv run poseidon review <pr-number> --detach              # review PR by hand; park on the gate
```

Every command's `--help`, rendered from the CLI itself at build time:

### `poseidon`

```text
Usage: poseidon [OPTIONS] COMMAND [ARGS]...

Options:
  --help  Show this message and exit.

Commands:
  login      Mint a token for a human the chart declared, with your own...
  whoami     Who the console takes you for, and which company it hosts.
  run        Start one implement run by hand on a ticket the company filed...
  review     Start one review run by hand on a delivered pull request — an...
  signal     Answer a gate a run parked on — `merge` or `budget`.
  cancel     Cancel a run; its teardown compensates.
  status     The run's record and its workflow's execution.
  story      The run's audit story — every decision, every source, in order.
  spend      The run's attributed spend.
  blueprint  Preview and apply company blueprints.
  heartbeat  The company's standing loop.
```

### `poseidon login`

```text
Usage: poseidon login [OPTIONS] {name}

  Mint a token for a human the chart declared, with your own cluster
  credentials, and print it: `export POSEIDON_TOKEN=$(poseidon login owner)`.
  The token is for the console's audience only — worthless anywhere else.

Arguments:
  name  [required]

Options:
  --namespace <str>  The platform namespace the chart installed into  [default:
                     poseidon-platform]
  --duration <int>   Token lifetime, seconds  [default: 3600]
  --help             Show this message and exit.
```

### `poseidon whoami`

```text
Usage: poseidon whoami [OPTIONS]

  Who the console takes you for, and which company it hosts.

Options:
  --help  Show this message and exit.
```

### `poseidon run`

```text
Usage: poseidon run [OPTIONS] {issue_number}

  Start one implement run by hand on a ticket the company filed — an operator's
  repro. The heartbeat starts the company's runs on its own.

Arguments:
  issue_number  [required]

Options:
  --detach / --no-detach  Start the run and exit  [default: no-detach]
  --help                  Show this message and exit.
```

### `poseidon review`

```text
Usage: poseidon review [OPTIONS] {pr_number}

  Start one review run by hand on a delivered pull request — an operator's
  repro, like `run`.

Arguments:
  pr_number  [required]

Options:
  --detach / --no-detach  Start the run and exit — the merge gate parks it until
                          `poseidon signal <run_id> merge` answers  [default:
                          no-detach]
  --help                  Show this message and exit.
```

### `poseidon signal`

```text
Usage: poseidon signal [OPTIONS] {run_id} {gate}

  Answer a gate a run parked on — `merge` or `budget`. The console records the
  verdict under your name, then delivers it.

Arguments:
  run_id  [required]
  gate    [required]

Options:
  --approved / --no-approved  The verdict: --approved or --no-approved
                              [default: approved]
  --payload <str>             A note recorded with the verdict
  --help                      Show this message and exit.
```

### `poseidon cancel`

```text
Usage: poseidon cancel [OPTIONS] {run_id}

  Cancel a run; its teardown compensates.

Arguments:
  run_id  [required]

Options:
  --reason <str>  Why
  --help          Show this message and exit.
```

### `poseidon status`

```text
Usage: poseidon status [OPTIONS] {run_id}

  The run's record and its workflow's execution.

Arguments:
  run_id  [required]

Options:
  --help  Show this message and exit.
```

### `poseidon story`

```text
Usage: poseidon story [OPTIONS] {run_id}

  The run's audit story — every decision, every source, in order.

Arguments:
  run_id  [required]

Options:
  --help  Show this message and exit.
```

### `poseidon spend`

```text
Usage: poseidon spend [OPTIONS] {run_id}

  The run's attributed spend.

Arguments:
  run_id  [required]

Options:
  --help  Show this message and exit.
```

### `poseidon blueprint`

```text
Usage: poseidon blueprint [OPTIONS] COMMAND [ARGS]...

  Preview and apply company blueprints.

Options:
  --help  Show this message and exit.

Commands:
  preview  Show what applying BLUEPRINT would change, and what it would cost.
  apply    Apply BLUEPRINT: the console compiles it against the live...
```

### `poseidon blueprint preview`

```text
Usage: poseidon blueprint preview [OPTIONS] {blueprint}

  Show what applying BLUEPRINT would change, and what it would cost.

Arguments:
  blueprint  [required]

Options:
  --help  Show this message and exit.
```

### `poseidon blueprint apply`

```text
Usage: poseidon blueprint apply [OPTIONS] {blueprint}

  Apply BLUEPRINT: the console compiles it against the live registry and submits
  the CRs; the operator converges them.

Arguments:
  blueprint  [required]

Options:
  -y, --yes  Skip the diff confirmation
  --help     Show this message and exit.
```

### `poseidon heartbeat`

```text
Usage: poseidon heartbeat [OPTIONS] COMMAND [ARGS]...

  The company's standing loop.

Options:
  --help  Show this message and exit.

Commands:
  state  The standing loop's execution and its ticks.
  wake   Nudge the loop to look now — never an instruction.
```

### `poseidon heartbeat state`

```text
Usage: poseidon heartbeat state [OPTIONS]

  The standing loop's execution and its ticks.

Options:
  --since <str>  Ticks at or after this timestamp
  --help         Show this message and exit.
```

### `poseidon heartbeat wake`

```text
Usage: poseidon heartbeat wake [OPTIONS]

  Nudge the loop to look now — never an instruction.

Options:
  --help  Show this message and exit.
```
