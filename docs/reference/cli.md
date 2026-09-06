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

<!-- gen: cli -->
