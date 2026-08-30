# Getting started

The hands-on companion to the [root README](../README.md), which teaches the
ideas this guide assumes (sections 1–5).

By the end of this guide you will have a company running on a local
cluster, watched it turn a goal into a merged pull request on a repository
you own, and — if you grant it the authority — seen it start its own work
on a heartbeat and merge its own passing changes. It costs nothing: the
default brain is a deterministic mock, so you can learn the whole shape of
the system before spending a cent on a real model.

The path: install → bring up a cluster → configure the company →
(optionally) point it at a real model → drive a run yourself → hand over
the keys, one at a time. Each step is below.

### Prerequisites

| Tool | Why |
|---|---|
| [`uv`](https://docs.astral.sh/uv/) | Python workspace and dependency management |
| Python 3.12 | |
| Docker | Building images; hosting the kind cluster |
| [`kind`](https://kind.sigs.k8s.io/) | Local Kubernetes |
| `kubectl`, `helm` | Cluster interaction and chart install |

You will also need a **disposable GitHub repository**. The agents open real
pull requests against it. Do not point this at anything you care about.

### Install and run the unit tests

```bash
uv sync
uv run pytest packages/common
```

> [!IMPORTANT]
> **Tests run per package, never repo-wide.** Every package has its own
> `tests/conftest.py`, and pytest cannot collect several modules with the
> same basename into one rootdir. `uv run pytest packages/` fails at
> collection — that is expected, not a broken checkout.

```bash
# all eleven suites
for p in packages/*/; do uv run pytest "$p" -q; done
```

`packages/registry` reports every test skipped without `REGISTRY_DSN`.
That is green: those tests exercise real SQL, so they need a real
Postgres, and a fake would prove nothing.

### Bring up the cluster

```bash
cp deploy/kind/.env.example deploy/kind/.env
# fill in GIT_TOKEN (repo-scoped PAT) and TARGET_REPO (host/path, no scheme)

deploy/kind/kind-up.sh
```

That script is idempotent — re-run it after changing code or the chart. It
creates the kind cluster, installs Calico (kind's default CNI does **not**
enforce NetworkPolicy, which would make containment tests pass vacuously),
builds and side-loads the eight images, installs the chart and Temporal,
applies the CRDs, and applies the example blueprint.

> [!TIP]
> **This costs nothing to run.** The cluster deploys the deterministic mock
> brain by default, so the full end-to-end suite spends zero LLM tokens.
> A real provider is opt-in — see [Use a real LLM](../README.md#use-a-real-llm) — and
> CI never turns it on.

### Configure the company

A company's entire configuration is its blueprint — one YAML document
declaring the standing agents, crews, tools, workflows, gates, budgets and policies.
There is no second configuration surface: anything the platform enforces,
you change by editing the document and re-applying it.
[The blueprint reference](blueprint.md) reads the shipped document line
by line; this guide is about operating it.

The bootstrap applies [`examples/hello-saas.yaml`](../examples/hello-saas.yaml)
for you. The repo ships the same company in three profiles, each granting
one more piece of authority than the last:

| Blueprint | Adds | Division of labour |
|---|---|---|
| [`hello-saas.yaml`](../examples/hello-saas.yaml) | — | A human starts every run and answers every merge. |
| [`hello-saas-heartbeat.yaml`](../examples/hello-saas-heartbeat.yaml) | a `heartbeat` section | The company starts its own runs on a heartbeat; a human feeds goals in and answers merges. |
| [`hello-saas-delegated.yaml`](../examples/hello-saas-delegated.yaml) | a policy-resolved merge gate and a contract tree | In-class passing work merges itself; a human feeds goals in and keeps the carve-outs and the oracle. |

All three declare the same company name, so moving between them changes the
running company in place — and moving back is the same command with the
earlier file. Revocation is a diff:

```bash
uv run poseidon blueprint preview examples/hello-saas-heartbeat.yaml  # what would change
uv run poseidon blueprint apply examples/hello-saas-heartbeat.yaml --yes
```

`preview` diffs the compiled plan against the live registry, so hiring,
firing and every authority change are visible before they happen. To make
the company yours, edit the document — the mission, the sandbox image and
test command, the `egress_allow` list, the budgets, the gate resolvers —
and re-apply. Mistakes fail at compile time with a readable error: a
standing executor, an undeclared tool, or a delegated path overlapping a
protected surface is refused before anything reaches the cluster.

### Use a real LLM

The mock brain is what the acceptance suite is written against. To run
`hello-saas` (or any other example company) with a real model, the cluster
must be brought up with `REAL_LLM=1`. Nothing does that for you.

The provider key never enters an agent pod. It lands only in the
`llm-proxy`, behind the LLM gateway; LiteLLM is started in single-model
mode, so every role — PM, planner, executor, reviewer — is served the
same `MODEL`.

Edit `deploy/kind/.env` (create it from `.env.example` if you have not
already). Uncomment and fill:

```bash
REAL_LLM=1
LLM_API_KEY=sk-your-provider-key
MODEL=gpt-5.6-luna
```

`gpt-5.6-luna` is the cheap OpenAI default this repo is wired for. Then
re-run the bootstrap so the proxy, the secret, and the tier map all
change together:

```bash
deploy/kind/kind-up.sh
```

To compare against Anthropic, swap **both** the key and the model —
LiteLLM copies `LLM_API_KEY` into `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
and reads the one that matches the model prefix, so leaving an OpenAI key
in place while changing `MODEL` to Claude will fail:

```bash
REAL_LLM=1
LLM_API_KEY=sk-ant-your-anthropic-key
MODEL=anthropic/claude-sonnet-4-5
```

Re-run `kind-up.sh` after every provider or model change.

Do not point `scripts/acceptance.sh` at a paid provider: the suite asserts
mock-brain behaviour, not model quality, and against a live model it would
spend money — so it refuses to run. Both the script's preflight and the
suite's own session guard read the *deployed* `llm-proxy` image (never
`.env`'s `REAL_LLM` claim) and fail fast on a paid brain unless
`POSEIDON_ALLOW_PAID=1` deliberately opts into spending. The command
reference is in [INTERFACES.md](../INTERFACES.md). Once the real model is up,
the walkthrough below is the full SDLC.

### A full run: a small Tetris app

This is intake → implement → review → human-gated merge, against the
disposable `TARGET_REPO`, with a real model already configured. The
company is still `hello-saas`; the product under construction is whatever
that repository holds. The engine is standard-library Python on purpose:
the sandbox image has pytest, and the agent prompt forbids `pip install`.

Leave a Temporal port-forward up for every CLI command. Do not pass
`--fast-timeouts` — those bounds are sized for the mock brain.

```bash
kubectl -n poseidon-platform port-forward svc/temporal-frontend 7233:7233
```

#### 1. Intake

A goal is untrusted text handed to the PM. The PM files tickets, comments
its summary of the scoping on each, and records a `feature-delivery` crew
request; it cannot write code.
[`examples/goal-tetris.md`](../examples/goal-tetris.md) is that goal:

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

#### 2. Implement

The ticket intake filed *is* the spec — start implement from its issue
number and the workflow resolves the open issue under the platform
identity, composes the task from it, and runs the delivery pipeline. The
issue body is untrusted input, exactly like any task file:

```bash
uv run poseidon run 42
```

A file path still works for ad-hoc tasks that never went through intake —
[`examples/task-tetris.md`](../examples/task-tetris.md) is such a spec:
`tetris.py` plus `tests/test_tetris.py`, on a `feature/*` branch. The
default workflow is implement:

```bash
uv run poseidon run examples/task-tetris.md
```

The crew planner writes a brief, the executor implements it, the
*platform* runs `pytest -q` as the verify gate of record, and only a
green verify releases the deliver dispatch that opens the pull request.
Success JSON has `"status": "done"` and a `pr_url` whose head ref matches
`^feature/[a-z0-9-]+$`. A push to `main` is a compiled deny.

If the per-run budget (`budgets.per_run.usd: 5` in the blueprint) trips,
the run parks in `paused-budget_exhausted`. Raise the cap, re-apply, then
`uv run poseidon signal <run_id> budget --approved --workflow implement`.

#### 3. Review and merge

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

### Hand over the keys: the heartbeat

Everything so far had you pressing start. To let the company run its own
backlog, apply the heartbeat profile:

```bash
uv run poseidon blueprint apply examples/hello-saas-heartbeat.yaml --yes
```

No command starts the heartbeat — that is the point. The orchestrator
worker notices that the applied blueprint now declares a `heartbeat` section
and starts one durable workflow, `heartbeat-hello-saas`, on its own. Every
`interval_minutes` (30 in the example — the cadence is yours to declare)
a tick:

1. **Observes** the open backlog, the delivered-but-unreviewed PRs (still
   open on the git host — one you merged by hand simply drops out), the
   in-flight runs and today's spend.
2. **Dispatches the PM in a toolless *decide* mode** for exactly one
   action — implement a ticket, review a PR, or idle — with a required
   one-line rationale, journaled as a `heartbeat.decide` audit event.
3. **Starts the chosen run** only if it wins the compiled `heartbeat.start`
   decision: within `max_concurrent_runs`, under the `daily_cap`, on an
   item fewer than `max_attempts` runs have taken up, and only a workflow
   on the `may_start` list. `intake` is deliberately not on that list —
   goals always enter through a human.

A run that dies before concluding anything (the model call timed out, the
reviewer hit its step cap) does not take its ticket or PR down with it:
the item is offered again until `max_attempts` runs have tried, and the
run that spends the last attempt leaves a comment on it saying so.

Your job shrinks to two acts: feed goals in
(`poseidon run --workflow intake …`) and answer merge gates. Watch it work:

```bash
uv run poseidon status hello-saas --workflow heartbeat   # the standing workflow
kubectl -n poseidon-platform logs -f deploy/orchestrator-worker
```

To take the keys back, apply a blueprint without the section —
`hello-saas.yaml` — and the heartbeat stops itself: each tick re-reads the
applied blueprint and exits when the section is gone, and the compiled
`heartbeat.start` rules vanish with it. `tests/e2e_kind/test_overnight.py`
runs this arrangement end to end with exactly two human acts: one goal in,
one approval out.

### Hand over the merge: the delegated gate

The one human act each run still needs is the merge approval. The flagship
profile delegates it — narrowly:

```bash
uv run poseidon blueprint apply examples/hello-saas-delegated.yaml --yes
```

Three things change relative to the heartbeat profile:

- `gates.merge.resolved_by` becomes `policy`, with a `delegate` block
  naming the path class the company may merge on its own
  (`slugify.py`, `tests/unit/`) and a rework bound (`max_reworks: 1`).
- A `policies.contract_tests` block declares the oracle: `tests/contract/`
  holds the acceptance spec delegated merges are graded by, and it can
  never be delegated.
- A new `contract-authoring` crew — the only role that may deliver into
  the contract tree — joins the PM's `may_spawn` list, so contract-first
  work can be sequenced.

A review run of an in-class PR does not park. When the independent
verdict passed, the platform's own re-run of the tests and the contract
baseline are green, and every changed path is in class, the orchestrator
merges with no human command —
[section 5](../README.md#the-merge-gate-human-or-policy) lists the facts and the
fallbacks. Everything else still stops for you: an out-of-class path, a
carve-out surface, and any edit to the contract tree park on the same
`merge` signal you answered above, and a failed quality layer feeds the
bounded rework loop instead.

> [!NOTE]
> The delegated class is yours to declare, and the example's is scoped to
> the acceptance suite's product (`slugify.py`). A Tetris PR from the
> walkthrough above touches `tetris.py` — out of class, so it declines to
> your `merge` signal. Edit `delegate.paths` to name the paths you are
> willing to see merge on green.

With both keys handed over, the human surface is a single act:
`tests/e2e_kind/test_delegated.py` proves the arc — one goal in, and
the company scopes, implements, reviews and merges its own in-class
passing work, every decision on the audit record. Taking the merge back is
the same diff in reverse: re-apply the heartbeat profile and every merge
parks on you again.

### Run the acceptance suite

```bash
scripts/acceptance.sh
```

The full end-to-end suite, under an hour, against the live cluster (the
exact count is in the pytest summary). It opens real pull requests on your
disposable repo — and owns that repo's state entirely: it seeds `main` to
a declared baseline, closes every PR and branch it creates, and finishes
by *asserting* the cluster is quiet, so two consecutive runs are green
with nothing done between them and a fresh disposable repo needs no
manual setup (`tests/e2e_kind/conftest.py` documents the contract). This
is the suite that proves the claims in this document — including the
red-team modules, which attack a real provisioned sandbox and a real
compiled merge gate.

```bash
deploy/kind/kind-down.sh    # when you are done
```
