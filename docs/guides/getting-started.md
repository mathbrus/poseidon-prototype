# Getting started

By the end of this guide you will have a company running on a local
cluster and watched it turn a goal into a merged pull request on a
repository you own. [Hand over the keys](hand-over.md) then lets it start
its own work on a heartbeat and merge its own passing changes. It costs
nothing: the default brain is a deterministic mock, so you can learn the
whole shape of the system before spending a cent on a real model.

The path: install → bring up a cluster → configure the company →
(optionally) [point it at a real model](real-llm.md) → drive a run
yourself. [The argument](../argument/problem.md) teaches the ideas this
guide assumes; you do not need it to follow the commands.

## Prerequisites

| Tool | Why |
|---|---|
| [`uv`](https://docs.astral.sh/uv/) | Python workspace and dependency management |
| Python 3.12 | |
| Docker | Building images; hosting the kind cluster |
| [`kind`](https://kind.sigs.k8s.io/) | Local Kubernetes |
| `kubectl`, `helm` | Cluster interaction and chart install |

You will also need a **disposable GitHub repository**. The agents open real
pull requests against it. Do not point this at anything you care about.

## Install and run the unit tests

```bash
uv sync
for p in packages/*/; do uv run pytest "$p" -q; done   # all eleven suites, no cluster needed
```

Tests run per package, never repo-wide — `uv run pytest packages/` fails
at collection by design, and the registry suite skips without a Postgres.
[Contributing](../developing/contributing.md) explains both.

## Bring up the cluster

```bash
cp deploy/kind/.env.example deploy/kind/.env
# fill in GIT_TOKEN (a repo-scoped PAT); TARGET_REPO is the disposable
# repository the e2e suite works on. The company's own repository is a
# blueprint field: set `stack.repo.url` in the example before applying it.

deploy/kind/kind-up.sh
```

That script is idempotent — re-run it after changing code or the chart. It
creates the kind cluster, installs Calico (kind's default CNI does **not**
enforce NetworkPolicy, which would make containment tests pass vacuously),
builds and side-loads the eight images, installs the chart and Temporal,
applies the CRDs, and applies the example blueprint.

!!! tip "This costs nothing to run"
    The cluster deploys the deterministic mock brain by default, so the
    full end-to-end suite spends zero LLM tokens. A real provider is
    opt-in — see [Use a real model](real-llm.md) — and CI never turns it on.

## Configure the company

A company's entire configuration is its blueprint — one YAML document
declaring the standing agents, crews, tools, workflows, gates, budgets and
policies. There is no second configuration surface: anything the platform
enforces, you change by editing the document and re-applying it.
[The blueprint](../reference/blueprint.md) reads the shipped document
section by section; this guide is about operating it.

The bootstrap applies `examples/hello-saas.yaml` for you. The repo ships the
same company in two profiles:

| Blueprint | Adds | Division of labour |
|---|---|---|
| [`hello-saas.yaml`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/hello-saas.yaml) | — | A human starts every run and answers every merge. |
| [`hello-saas-delegated.yaml`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/hello-saas-delegated.yaml) | a `heartbeat` section, a policy-resolved merge gate and a contract tree | The company starts its own runs and merges in-class passing work itself; a human feeds goals in and keeps the carve-outs and the oracle. |

Both declare the same company name, so moving between them changes the
running company in place — and moving back is the same command with the
earlier file. Revocation is a diff:

```bash
uv run poseidon blueprint preview examples/hello-saas-delegated.yaml  # what would change
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

`preview` diffs the compiled plan against the live registry, so hiring,
firing and every authority change are visible before they happen. To make
the company yours, edit the document — the mission, the sandbox image and
test command, the `egress_allow` list, the budgets, the gate resolvers —
and re-apply. Mistakes fail at compile time with a readable error: a
standing implementer, an undeclared tool, or a delegated path overlapping a
protected surface is refused before anything reaches the cluster.

## A full run: a small Tetris app

This is intake → implement → review → human-gated merge, against the
repository the applied blueprint declares (`stack.repo`), with a
[real model](real-llm.md) already configured. The company is still
`hello-saas`; the product under construction is whatever that repository
holds. The engine is standard-library Python on purpose: the sandbox image
has pytest, and the agent prompt forbids `pip install`.

Leave a Temporal port-forward up for every CLI command. Activity bounds
come from the deployment's `TIMEOUT_PROFILE`, so a cluster brought up with
`REAL_LLM=1` already runs the generous profile a paid brain needs.

```bash
kubectl -n poseidon-platform port-forward svc/temporal-frontend 7233:7233
```

### 1. Intake

A goal is untrusted text handed to the PM. The PM files tickets, comments
its summary of the scoping on each, and records a `feature-delivery` crew
request; it cannot write code.
[`examples/goal-tetris.md`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/goal-tetris.md)
is that goal:

```bash
uv run poseidon run --workflow intake examples/goal-tetris.md
```

The CLI prints `started intake-<run_id> (run_id …)` on stderr and blocks
until the run finishes. Success is JSON with `"status": "done"`,
`"workflow": "intake"`, one or more `ticket_urls`, and a
`crew_request_id`. The issues are real on `TARGET_REPO`, each with the PM's
summary as its first comment. hello-saas declares no intake gate, so there
is no signal to send.

Intake and implement are **separate runs**. Fulfilment keys crew requests
by `run_id`, so implement does not consume the intake row; a human picks
which ticket to run — choosing is scheduling — and implement consumes the
ticket itself.

### 2. Implement

The ticket intake filed *is* the spec — start implement from its issue
number and the workflow resolves the open issue under the platform
identity, composes the task from it, and runs the delivery pipeline. The
issue body is untrusted input, exactly like any task file:

```bash
uv run poseidon run 42
```

A file path still works for ad-hoc tasks that never went through intake —
[`examples/task-tetris.md`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/task-tetris.md)
is such a spec: `tetris.py` plus `tests/test_tetris.py`, on a `feature/*`
branch. The default workflow is implement:

```bash
uv run poseidon run examples/task-tetris.md
```

The crew's implementer implements the task, the
*platform* runs `pytest -q` as the verify gate of record, and only a
green verify releases the deliver dispatch that opens the pull request.
Success JSON has `"status": "done"` and a `pr_url` whose head ref matches
`^feature/[a-z0-9-]+$`. A push to `main` is a compiled deny.

If the per-run budget (`budgets.per_run.usd: 5` in the blueprint) trips,
the run parks in `paused-budget_exhausted`. Raise the cap, re-apply, then
`uv run poseidon signal <run_id> budget --approved --workflow implement`.

### 3. Review and merge

Implement stops at an open PR. Review provisions a fresh sandbox from the
PR head, dispatches a reviewer that holds no repository-writing tools, and
parks on the merge gate — so `--detach` is the usual mode. The number is
the trailing integer of `pr_url`:

```bash
uv run poseidon review <pr-number> --detach
# started review-<review_run_id> (run_id …)
uv run poseidon status <review_run_id> --workflow review
```

Wait until findings are posted on the PR and status is paused on the
gate, then approve or reject. Use the **review** `run_id`, not the
implement one. Approval merges as the orchestrator, under the platform
identity; the CLI never touches the repository:

```bash
uv run poseidon signal <review_run_id> merge --workflow review
# uv run poseidon signal <review_run_id> merge --no-approved --workflow review
```

Watch a live run with `kubectl -n poseidon-platform logs -f deploy/orchestrator-worker`
and `kubectl -n hello-saas get pods -w`. Teardown is guaranteed: on every
terminal path the crew identities, pods, and sandbox go away.

Every command and flag is on [the CLI reference](../reference/cli.md).
Next: [hand over the keys](hand-over.md).
