# Quickstart

In this guide you run a company on a local Kubernetes cluster. You watch
it write its own strategy, open a pull request on your repository, and
wait for you to approve the merge.

!!! tip "Free, about 20 minutes"
    The cluster uses a free, deterministic mock model, so nothing here
    costs money. Most of the time goes into building the images the first
    time.

## Before you start

| You need | Why |
|---|---|
| [uv](https://docs.astral.sh/uv/) and Python 3.12 | Installs Poseidon and runs the `poseidon` CLI. |
| Docker | Builds the images and hosts the cluster. |
| [kind](https://kind.sigs.k8s.io/), `kubectl` and `helm` | Run Kubernetes locally and install the platform. |
| About 20 GB of free disk | The cluster and its images. |
| A **disposable** GitHub repository, with at least one commit on `main` | The agents open real pull requests and issues on it. Don't use a repository you care about. |
| A GitHub token for that repository | It must push branches and manage pull requests and issues. A classic personal access token with the `repo` scope works. |

## 1. Install

Clone the repository and install the workspace:

```bash
git clone https://github.com/mathbrus/poseidon-prototype.git
cd poseidon-prototype
uv sync
```

## 2. Configure the cluster

Copy the example environment file:

```bash
cp deploy/kind/.env.example deploy/kind/.env
```

Open `deploy/kind/.env` and set your token:

```bash
GIT_TOKEN=github_pat_your-token
```

Leave the rest as it is. `REAL_LLM` stays commented out, so the cluster
uses the mock model.

## 3. Create the cluster

```bash
deploy/kind/kind-up.sh
```

The script creates a kind cluster, builds the images, installs the
platform and signs you in. It prints eight steps and the time each took:

```text
[1/8] preflight
      docker: 20 GiB free under /var/lib/docker
      cluster poseidon, platform namespace poseidon-platform, build tag 89415694df84
      version v0.2.0+2026.10.21-3-g02ecb48: what the images will answer to
      humans: owner auditor — you will be owner
      brain: mock (poseidon-mock-brain), zero spend
      sandbox image: poseidon-sandbox:89415694df84
      done in 2s
[2/8] cluster
      kind cluster 'poseidon' created
      done in 16s
[3/8] CNI (Calico, so NetworkPolicy is enforced)
      done in 47s
[4/8] images
      9 images built at 89415694df84
      loaded into the cluster
      done in 522s
[5/8] chart (CRDs, then the Poseidon release)
      release poseidon installed; humans declared: owner auditor
      done in 3s
[6/8] platform rollout
      postgres, opa, llm-proxy, llm-gateway, mcp-gateway, egress-proxy, bundle-server, reconciler ready
      done in 47s
[7/8] Temporal (chart 1.6.0)
      temporal, the orchestrator worker and the console ready
      done in 48s
[8/8] verify
      every platform pod runs 89415694df84
      gateway and console refuse without a token and know owner with one
      done in 6s
==> Poseidon v0.2.0+2026.10.21-3-g02ecb48 up in 691s in poseidon-platform
    console http://127.0.0.1:8090, gateway http://127.0.0.1:8080
    you are owner for a week: `uv run poseidon whoami`
    next: `uv run poseidon blueprint apply <blueprint>` (examples/hello-saas.yaml to try the example)
```

You are signed in as `owner`. The token is saved in
`~/.config/poseidon/token` for a week.

## 4. Write the blueprint

Copy the example blueprint:

```bash
cp examples/hello-saas.yaml company.yaml
```

Open `company.yaml` and point `stack.repo.url` at your disposable
repository:

```yaml
stack:
  repo:
    url: github.com/<you>/<your-disposable-repo>
    base_ref: main
```

The rest of the file is the company:

- `company.vision` is what it builds. Keep the example's for now.
- `company.humans` lists who may act on it: you, as `owner`.
- `workflows.review.gates.merge.resolved_by: human` means every merge waits
  for you.
- `budgets` and `heartbeat` cap what it spends and how much it does at once.

## 5. Apply it

```bash
uv run poseidon blueprint apply company.yaml --yes
```

`apply` shows what it creates, applies it, and waits until the company's
policy is live:

```text
hello-saas: 1 change(s)
  + pm-1
      role pm
  budget: $5/run
applied hello-saas: 2 created, 0 updated (blueprint_apply_bootstrap)
activating …
converged (10 s)
```

Check who you are and what the cluster hosts:

```bash
uv run poseidon whoami
```

```text
you are owner (human)
company: hello-saas · converged
```

## 6. Watch it work

```bash
uv run poseidon follow
```

The company starts at once. Its repository has no strategy and no
architecture plan yet, so the first thing it does is file two tickets to
write them. An agent called the *steward* writes each document and opens a
pull request. A reviewer agent checks it, and the run parks, waiting for
your verdict:

```text
19:41:00  -                             human/owner                 blueprint.apply  allow
19:42:03  heartbeat                     platform/orchestrator       heartbeat.bootstrap  allow (bootstrap_filed:strategy)
19:42:04  heartbeat                     platform/orchestrator       heartbeat.bootstrap  allow (bootstrap_filed:architecture)
19:42:04  heartbeat                     platform/orchestrator       heartbeat.tick  deny (tick_idle:bootstrap_filed:strategy,architecture)
19:42:08  heartbeat                     platform/orchestrator       heartbeat.decide  allow (decision_valid)
          ticket #2556 is the oldest open work and nothing is waiting on review.
19:42:09  heartbeat                     platform/orchestrator       heartbeat.tick  allow (tick_started:implement_2556_as_auto-c8357daa)
…
19:42:54  auto-c8357daa                 steward/stewardship-steward-auto-c8357daa  git.open_pr  allow
          The branch carries the task's change with its tests; the suite ran green in the sandbox, so the work is complete and delivered.
19:43:05  heartbeat                     platform/orchestrator       heartbeat.decide  allow (decision_valid)
          PR #2558 is delivered and unreviewed; a review unblocks the merge gate before new work starts.
19:43:05  heartbeat                     platform/orchestrator       heartbeat.tick  allow (tick_started:review_2558_as_auto-3d73e674)
…
19:43:45  auto-3d73e674                 reviewer/feature-review-reviewer-auto-3d73e674  git.comment  allow
```

The rows marked `…` are left out here: every tool call and model call prints
as its own row. The mock model reports a small simulated cost so the
budget machinery runs, which is why `spend` moves. Nothing is billed.

Press Ctrl+C to stop following. The company keeps working.

## 7. Approve the merge

Find the review run that waits for you:

```bash
uv run poseidon runs
```

```text
run             workflow    task                        status    started     took
──────────────────────────────────────────────────────────────────────────────────
auto-3d73e674   review      review pull request #2558   running   0 min ago   53 s
auto-c8357daa   implement   implement issue #2556       done      1 min ago   54 s
```

The review run is the one still `running`. Read the pull request on
GitHub, then approve it with the review run's id:

```bash
uv run poseidon verdict <run_id> merge approve --reason "the strategy reads well"
```

```text
verdict recorded: merge approved for run auto-3d73e674, by owner
```

The pull request merges. Once both documents are on `main`, the company
starts turning its strategy into tickets and code. Each delivered pull
request waits for your verdict the same way.

## 8. Clean up

Delete the company, then the cluster:

```bash
uv run poseidon company delete --reason "done with the quickstart" --yes
deploy/kind/kind-down.sh
```

```text
hello-saas: 1 running run(s) — they will be cancelled:
run             workflow    task
─────────────────────────────────────────────────
auto-f0fa3f44   implement   implement issue #2557
hello-saas deleted in 12 s; cancelled auto-f0fa3f44
```

Your repository keeps the pull requests and issues the company opened.

## Next steps

- [Build a game with a real model](tutorial.md): the same loop, doing real work.
- [Answer gates and follow a run](../how-to/gates-and-runs.md): every command for watching and steering a company.
- [How it works](../explanation/how-it-works.md): what happened behind each step.
