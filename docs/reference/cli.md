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
workflows, which is why several commands take a `--workflow` option: the
CLI needs to know which workflow a run belongs to in order to address it.
The fourth workflow is not run-shaped: the heartbeat is one standing
workflow per company, `heartbeat-{company}`, started by the worker's
ensure loop whenever the applied blueprint declares a `heartbeat` section
and self-stopping when the section is gone (`poseidon status <company>
--workflow heartbeat` inspects it). No human starts it, which is the
point — and its authority is exactly the compiled `heartbeat.start`
decision, nothing more.

The canonical human acts:

```bash
uv run poseidon run --workflow intake goal.md            # a goal in
uv run poseidon run 42                                   # implement ticket #42
uv run poseidon review <pr-number> --detach              # review, then park on the gate
uv run poseidon signal <run_id> merge --workflow review  # approve the merge
uv run poseidon signal <run_id> merge --no-approved --workflow review   # reject
```

Every command's `--help`, rendered from the CLI itself at build time:

<!-- gen: cli -->
