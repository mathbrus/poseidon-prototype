# Getting started

By the end of this guide you will have a company running on a local
cluster and watched it turn its own strategy into a merged pull request on a
repository you own. [Hand over the keys](../how-to/delegate-merges.md) then lets it start
its own work on a heartbeat and merge its own passing changes. It costs
nothing: the default brain is a deterministic mock, so you can learn the
whole shape of the system before spending a cent on a real model.

The path: install → bring up a cluster → configure the company →
(optionally) [point it at a real model](../how-to/real-model.md) → drive a run
yourself. [The argument](../explanation/security-model.md) teaches the ideas this
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

deploy/kind/kind-up.sh
```

That script installs the platform and knows nothing about any company. It
is idempotent — re-run it after changing code or the chart; a company
already applied stays applied and its standing agents re-roll onto the
new build. It creates the kind cluster, installs Calico (kind's default
CNI does **not** enforce NetworkPolicy, which would make containment tests
pass vacuously), builds and side-loads the nine images, applies the CRDs,
installs the chart and Temporal, checks every platform pod runs this
build, and logs you in.

It prints eight numbered steps with the time each took and the facts that
matter — who you will be, which brain (the mock, or a paid model marked as
such) — and keeps what the tools print in `deploy/kind/kind-up.log`, whose
tail it shows when a step fails; `VERBOSE=1` streams it all instead.

The token it logged in with is saved for you, a week long, in your config
directory (`~/.config/poseidon/token`), and every `poseidon` command reads
it there when `POSEIDON_TOKEN` is unset. Applying a company is your act,
the one command the bring-up leaves you:

```bash
uv run poseidon blueprint apply examples/hello-saas.yaml --yes   # returns once converged
uv run poseidon whoami
```

A cluster hosts one company, in the fixed namespace `poseidon-company`:
to apply another, delete the current one first (`poseidon company
delete`). On kind the people a blueprint may name are the chart's
(`owner` and `auditor`, from `values-kind.yaml`); the console refuses a
blueprint naming anyone else.

Every `poseidon` command from here on is a call to the console — the one
service a person acts on a company through — under that name; nothing
else is needed on your machine: no port-forward, no database address, no
cluster credentials in the CLI. To act as another human the chart
declares (on kind: `auditor`), mint a token of your own with the
kubeconfig you already hold and export it; the environment wins over the
file:

```bash
export POSEIDON_TOKEN=$(uv run poseidon login auditor)
```

!!! tip "This costs nothing to run"
    The cluster deploys the deterministic mock brain by default, so the
    full end-to-end suite spends zero LLM tokens. A real provider is
    opt-in — see [Use a real model](../how-to/real-model.md) — and CI never turns it on.

## Configure the company

A company's entire configuration is its blueprint — one YAML document
declaring the standing agents, crews, tools, workflows, gates, budgets and
policies. There is no second configuration surface: anything the platform
enforces, you change by editing the document and re-applying it.
[The blueprint](../reference/blueprint.md) reads the shipped document
section by section; this guide is about operating it.

The quickest way to a blueprint of your own is to answer questions:

```bash
uv run poseidon blueprint new            # writes <company-name>.yaml, then say how to apply it
```

It asks for what is yours to decide — the name, what the company should
build and why, who may steer it, the repository and its test command,
what the agents' workspace may reach, who decides whether a pull request
merges and which folders may merge on their own, the money — in plain
words, with the shipped example's answers as defaults so Enter walks
through; only the vision and the repository must be typed.
The shape every company repeats it writes without asking, and it compiles
the result the way the console would before writing a byte. Every value
in the file is yours to change afterwards.

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
(`stack.repo`), with a [real model](../how-to/real-model.md) already configured. The
company is `hello-saas`; the product under construction is whatever its
vision says. Nothing goes in but that vision: change it in the blueprint
to name the product you want, apply, and watch — literally, from a second
terminal, where every tick, run, tool call, decision and verdict lands as
one line and the spend is printed each time it moves:

```bash
uv run poseidon follow
```

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
choose; `humans` names who may act, drawn from the people the chart
declares. Activity timeouts come from the deployment's `TIMEOUT_PROFILE`,
so a cluster brought up with `REAL_LLM=1` already runs the generous
profile a paid brain needs. A third shipped example,
`examples/pirate-fight.yaml`, is a different company altogether — a
terminal game — and applies the same way.

```bash
uv run poseidon blueprint apply company.yaml --yes   # returns once converged; delete the example's company first if it is applied
uv run poseidon heartbeat show
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
uv run poseidon verdict <review_run_id> merge approve --reason "the strategy names a slice I want built"
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
watch — the human's commands start exactly the run the loop would:

```bash
uv run poseidon start implement 42
uv run poseidon start review <pr-number> --no-wait
```

### 4. Review and merge

The delivered pull request wakes the heartbeat, which starts the review
run: an ephemeral reviewer reads the diff and the documents, runs the
suite in its own sandbox, posts findings, judges the change against the
masterplan, and ends with a verdict. Under `hello-saas.yaml` the merge
gate is yours:

```bash
uv run poseidon verdict <review_run_id> merge approve --reason "engine first, as the vision asks"
```

The console records that verdict and your reason under your name before
it delivers the signal, and the release reads the record: policy merges
on a recorded approving verdict by a person the blueprint names, never
on the signal alone.

Every step is on the record: `uv run poseidon runs` for the company's
runs, `uv run poseidon run show <run_id>` for one, `uv run poseidon run
story <run_id> --decisions` for what it decided and why — your verdict
and its reason among them — and `uv run poseidon follow` to watch it all
land.
[Hand over the keys](../how-to/delegate-merges.md) is where the merge becomes the
company's too.
