# Getting started

By the end of this guide you will have a company running on a local
cluster and watched it turn its own strategy into a merged pull request on a
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
for p in packages/*/; do uv run pytest "$p" -q; done   # every package's suite (thirteen), no cluster needed
```

Tests run per package, never repo-wide — `uv run pytest packages/` fails
at collection by design, and the registry suite skips without a Postgres.
[Contributing](../developing/contributing.md) explains both.

## Bring up the cluster

```bash
cp deploy/kind/.env.example deploy/kind/.env
# fill in GIT_TOKEN (a repo-scoped PAT). The company's repository is a
# blueprint field, `stack.repo`, and nothing else names it.

deploy/kind/kind-up.sh                                    # the shipped example
BLUEPRINT=path/to/company.yaml deploy/kind/kind-up.sh     # your company
```

That script is idempotent — re-run it after changing code or the chart. It
validates the blueprint first (the compiler's own checks, before any
cluster work), creates the kind cluster, installs Calico (kind's default
CNI does **not** enforce NetworkPolicy, which would make containment tests
pass vacuously), builds and side-loads the nine images, installs the chart
and Temporal, applies the CRDs, and applies the blueprint through the
console as the first human it names. A cluster hosts one company, in the
fixed namespace `poseidon-company`: to bring up another, delete the
current one first. The token it applied with is saved for you, a week
long, in your config directory (`~/.config/poseidon/token`), and every
`poseidon` command reads it there when `POSEIDON_TOKEN` is unset:

```bash
uv run poseidon whoami
```

To act as another human the chart declares, mint a token of your own and
export it; the environment wins over the file:

```bash
export POSEIDON_TOKEN=$(uv run poseidon login auditor)
```

Every `poseidon` command from here on is a call to the console — the one
service a person acts on a company through — under that name. `login`
mints the token with the kubeconfig you already hold, for the human the
chart declares (`owner` on kind); nothing else is needed on your machine:
no port-forward, no database address, no cluster credentials in the CLI.

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
| [`hello-saas.yaml`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/hello-saas.yaml) | — | The company scopes, implements and reviews on its own; a human answers every merge. |
| [`hello-saas-delegated.yaml`](https://github.com/mathbrus/poseidon-prototype/blob/master/examples/hello-saas-delegated.yaml) | a policy-resolved merge gate and a contract tree | The company merges its own in-class passing work too; a human keeps the carve-outs and the oracle. |

Both declare the same company name, so moving between them changes the
running company in place — and moving back is the same command with the
earlier file. Revocation is a diff:

```bash
uv run poseidon blueprint preview examples/hello-saas-delegated.yaml  # what would change
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

`preview` diffs the compiled plan against the live registry, so hiring,
firing and every authority change are visible before they happen. To make
the company yours, edit the document — the vision, the sandbox image and
test command, the `egress_allow` list, the budgets, the gate resolvers —
and re-apply. Mistakes fail at compile time with a readable error: a
standing implementer, an undeclared tool, or a delegated path overlapping a
protected surface is refused before anything reaches the cluster.

## A full run: a small Tetris app

This is the whole loop — bootstrap, scope, implement, review, human-gated
merge — against the repository the applied blueprint declares
(`stack.repo`), with a [real model](real-llm.md) already configured. The
company is `hello-saas`; the product under construction is whatever its
vision says. Nothing goes in but that vision: change it in the blueprint
to name the product you want, apply, and watch.

```yaml
company:
  name: hello-saas
  vision: >-
    A playable, self-contained Tetris in Python, standard library only,
    built engine first — board, tetrominoes, gravity, line clears, a
    score — with pytest coverage, on a repository the company keeps green.
```

Put that `company:` block in place of the example's, in a copy of
`examples/hello-saas.yaml` saved as `company.yaml`. The name is yours to
choose; `humans` names who may act, and the first of them is whom kind-up
applies as. Activity bounds come from the deployment's `TIMEOUT_PROFILE`,
so a cluster brought up with `REAL_LLM=1` already runs the generous
profile a paid brain needs. A third shipped example,
`examples/pirate-game.yaml`, is a different company altogether — a
terminal game — and applies the same way.

```bash
BLUEPRINT=company.yaml deploy/kind/kind-up.sh   # on a fresh cluster, or
uv run poseidon blueprint apply company.yaml --yes   # on one that hosts this company
uv run poseidon heartbeat state
```

### 1. Bootstrap

The repository has no strategy and no architecture masterplan yet. The
heartbeat's first ticks observe that and file two tickets under the
platform identity — `Bootstrap: strategy`, `Bootstrap: architecture
masterplan` — routed to the stewardship crew. The steward writes each
document from the vision (the masterplan from the tree too) and delivers
it as a pull request. Under `hello-saas.yaml` those pull requests park on
your merge gate like any other:

```bash
uv run poseidon signal <review_run_id> merge
```

Until both documents are on `main`, the loop refuses to scope: the first
thing the company does is decide what it is going to do.

### 2. Scope

With the documents in place, the next tick's PM chooses `scope`: the loop
starts a scope run, the PM reads the strategy and the masterplan and files
the first slice — the game engine — as tickets on the company's repository, each with
its `Crew:` trailer and the PM's summary as its first comment. The
observation is untrusted text handed to the PM; it cannot write code, and
the platform refuses scoping past `max_open_tickets`.

### 3. Implement

The tickets the company filed *are* the spec. The heartbeat starts an
implement run on the lowest-numbered one; the workflow resolves the issue
under the platform identity, composes the task from it, and runs the
delivery pipeline with the documents beside the ticket. The crew's
implementer implements the slice, the *platform* runs `pytest -q` as the
verify gate of record, and only a green verify releases the deliver
dispatch that opens the pull request. The run record has `"status":
"done"` and a `pr_url` whose head ref matches `^feature/[a-z0-9-]+$`. A
push to `main` is a compiled deny; so is a change to the documents by
anyone but the steward.

To reproduce one run by hand — a ticket that failed, a review you want to
watch — the operator commands start exactly the run the loop would:

```bash
uv run poseidon run 42
uv run poseidon review <pr-number> --detach
```

### 4. Review and merge

The delivered pull request wakes the heartbeat, which starts the review
run: an ephemeral reviewer reads the diff and the documents, runs the
suite in its own sandbox, posts findings, judges the change against the
masterplan, and ends with a verdict. Under `hello-saas.yaml` the merge
gate is yours:

```bash
uv run poseidon signal <review_run_id> merge
```

The console records that verdict under your name before it delivers the
signal, and the release reads the record: policy merges on a recorded
approval by a person the blueprint names, never on the signal alone.

Every step is on the record: `uv run poseidon status <run_id>` for the
run and `uv run poseidon story <run_id>` for its whole story, every source
in order — your verdict among them.
[Hand over the keys](hand-over.md) is where the merge becomes the
company's too.
