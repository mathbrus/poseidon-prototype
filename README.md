# Poseidon PoC

One coding agent ships one pull request under policy: every side effect is
policy-checked, credential-free from the agent's perspective, and audited.

This repository is a small, runnable proof of that claim. A host-side **runner**
gives the agent a task file; the agent codes inside a disposable **sandbox**;
the only way code leaves the system is through an **MCP gateway** that checks
policy (OPA), holds the git credentials, opens the PR, and writes an audit
line for every tool call.

Full specification (architecture, exit criteria, contracts):
[markdowns/prototype.md](markdowns/prototype.md).

### What this PoC proves

| # | Claim | How you see it |
|---|---|---|
| E1 | Happy path: a real task becomes a real PR on `feature/*` | Demo §1 / `tests/e2e/test_happy_path.py` |
| E2 | Prompt injection to push `main` is denied; work still lands on `feature/*` | Demo §2 / `tests/e2e/test_injection.py` |
| E3 | Agent and sandbox have no unmediated internet; no git/LLM secrets in their env | Sanity check below / `tests/e2e/test_egress.py` + CI |
| E4 | Every gateway tool call is reconstructable from `runs/audit.jsonl` | Audit commands below / `tests/e2e/test_audit.py` |

### How a run works

```text
you ──► runner (CLI on the host)
          │
          ├─► gateway.repo.archive     fetch a clean workspace tarball
          ├─► Docker sandbox           unpack workspace, zero egress
          ├─► gateway /admin/sandboxes register sandbox URL for this run_id
          └─► agent POST /task         plan → act → verify → deliver
                │
                └─► gateway MCP tools  (policy → audit → proxy or git)
                      ├ sandbox.*  ──► sandbox tool server
                      └ git.open_pr ─► GitHub (platform token)
```

**Secrets live in exactly two places:** `GIT_TOKEN` in the gateway,
`LLM_API_KEY` in the llm-proxy. The agent only ever sees
`AGENT_BEARER_TOKEN` (identity for the gateway). Sandboxes hold no
credentials and no git remotes.

**Networks are the egress control.** Compose defines four Docker networks:

- `agentnet` (internal) — agent, gateway, llm-proxy; all the agent can reach
- `sandboxnet` (internal) — gateway ↔ per-run sandboxes; sandboxes have *no*
  internet (toolchain is prebaked into the image)
- `opanet` (internal) — gateway ↔ OPA
- `egress` — gateway and llm-proxy only; the only path to the internet

The runner runs on the host: it reaches the gateway on
`http://127.0.0.1:8080` (published port) and the agent at a fixed address on
`agentnet` (`AGENT_URL`, default `http://172.28.10.10:8000`).

### Pieces in this repo

| Piece | Role |
|---|---|
| `packages/runner` | CLI `poseidon-poc`: owns the implement flow and sandbox lifecycle |
| `packages/agent` | FastAPI + LangGraph loop (plan → act → verify → deliver) |
| `packages/gateway` | MCP server: auth, OPA policy, audit, sandbox proxy, `repo.archive` / `git.open_pr` |
| `packages/sandbox` | Tool server image + `DockerBackend` that provisions per-run containers |
| `packages/common` | Shared IDs, audit model, settings, JSON logging |
| `policies/` | OPA/Rego rules (default deny; `feature/*` only for PRs; runner-only archive) |
| `deploy/compose/` | Compose stack: opa, llm-proxy, mcp-gateway, agent |
| `examples/` | `task-happy.md` and `task-injected.md` (E1 / E2 demos) |
| `runs/` | Per-run manifests + append-only `audit.jsonl` (gitignored) |

---

## Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (daemon running),
  Docker Compose v2
- A **disposable** GitHub repo your PAT can push to and open PRs against
  (reruns create real branches and PRs)
- An LLM provider key (OpenAI- or Anthropic-compatible; see `MODEL` below)

## Setup

```bash
git clone <this-repo> && cd poseidon-prototype
uv sync

cp deploy/compose/.env.example deploy/compose/.env
# Edit deploy/compose/.env:
#   GIT_TOKEN      — repo-scoped PAT (gateway only)
#   LLM_API_KEY    — provider key (llm-proxy only)
#   TARGET_REPO    — host/path, no scheme (e.g. github.com/you/poseidon-poc-target)
#   MODEL          — e.g. gpt-4o-mini or anthropic/claude-haiku-4-5
# Bearer tokens (AGENT_BEARER_TOKEN, RUNNER_BEARER_TOKEN) can stay as placeholders.

# Sandbox image (toolchain prebaked — sandboxes have zero egress)
docker build -f Dockerfile.sandbox -t poseidon-sandbox .

# Bring the stack up (gateway, agent, OPA, llm-proxy)
cd deploy/compose
docker compose up -d --build --wait
docker compose ps    # opa, llm-proxy, mcp-gateway, agent → healthy
cd ../..
```

Load the same `.env` into your shell so the host-side runner can see it
(Compose reads the file on its own; the CLI does not):

```bash
set -a && source deploy/compose/.env && set +a
```

Quick sanity checks:

```bash
# Agent holds only its bearer token among token/key-shaped vars (E3)
docker compose -f deploy/compose/docker-compose.yml exec agent \
  env | grep -Ei 'token|key'
# → AGENT_BEARER_TOKEN=…

# Gateway answers 401 without a bearer (proves it is up)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/mcp
# → 401
```

---

## Demo

Two commands. Both need the stack healthy and the shell env loaded as above.
Each run prints a JSON **manifest** (`runs/<run_id>/manifest.json`) with
status, `pr_url`, iterations, and audit-line count.

### 1. Happy path → feature-branch PR (E1)

```bash
uv run poseidon-poc run examples/task-happy.md
```

On success: `"status": "done"` and a `pr_url` on a `feature/*` branch.
Re-check later with:

```bash
uv run poseidon-poc status <run_id>
```

### 2. Injected “push to main” → deny in audit, PR still on `feature/*` (E2)

```bash
uv run poseidon-poc run examples/task-injected.md
```

Same task as above, plus an embedded instruction to deliver on `main`.
Inspect the audit for that run:

```bash
jq 'select(.run_id == "<run_id>") | {tool, decision, rule, backend_status}' \
  runs/audit.jsonl
```

When the bait works you should see one or more
`decision=deny` / `rule=git_open_pr_protected_branch` lines for `git.open_pr`,
then an allow that produces a `feature/*` `pr_url` in the manifest.

Outcomes vary with the model and the disposable repo's state:

- A well-aligned model may refuse the bait and never attempt `main` — no deny
  line, but the PR still lands on `feature/*`.
- A model that keeps retrying `main` may exhaust delivery steps without a PR;
  the deny lines are still the demo artifact.
- An `allow` with `backend_status=error` on `git.open_pr` usually means the
  branch/PR already exists on the target repo — check gateway logs and use a
  fresh disposable repo (or delete the colliding branch).

Policy — not the prompt — is what makes a `main` delivery impossible when
attempted.

---

## Troubleshooting

**OPA decisions.** OPA is only on the internal `opanet`. Ask it through the
gateway container, or read the audit trail (usually enough):

```bash
# Live probe from the gateway's network namespace
docker compose -f deploy/compose/docker-compose.yml exec mcp-gateway \
  python -c "
import json, urllib.request
body = json.dumps({'input': {
  'agent_id': 'backend-engineer-1', 'role': 'backend-engineer',
  'run_id': 'run-none', 'tool': 'git.open_pr',
  'args': {'branch': 'main'},
}}).encode()
req = urllib.request.Request(
  'http://opa:8181/v1/data/poseidon/poc/decision',
  data=body, headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode())
"
# → {"result":{"allow":false,"rule":"git_open_pr_protected_branch"}}

# What the gateway actually decided for a run
jq 'select(.run_id == "<run_id>")' runs/audit.jsonl
```

**Audit tail.** The gateway appends every tool call (allow and deny) to
`runs/audit.jsonl` and to its stdout. That log is the E4 forensic artifact:

```bash
tail -f runs/audit.jsonl
docker compose -f deploy/compose/docker-compose.yml logs -f mcp-gateway
```

**Sandbox logs.** Per-run containers are named `poseidon-sbx-<run_id>` and are
destroyed at teardown. While a run is in flight:

```bash
docker logs -f poseidon-sbx-<run_id>
```

After teardown, use gateway proxy errors in `mcp-gateway` logs and the
`backend_status` field on audit lines.

**Agent / LLM.** The agent is only on `agentnet` (reachable from the host at
`AGENT_URL`):

```bash
docker compose -f deploy/compose/docker-compose.yml logs -f agent llm-proxy
curl -s "$AGENT_URL/task/<run_id>"
```

**Common failures**

| Symptom | Likely cause |
|---|---|
| `RunnerSettings` validation error on `run` | `.env` not sourced into the shell |
| Cannot reach agent / connection timeout | stack not up, or `AGENT_URL` wrong for your host |
| Sandbox provision fails on network | `SANDBOX_NETWORK` must be `poseidon-sandboxnet` (compose project network name) |
| Agent env has unexpected `*_KEY` | image rebuild needed; base `python:3.12-slim` ships `GPG_KEY` which compose clears |

---

## Developing further

```bash
uv run pytest                          # unit tests (docker/e2e markers excluded)
uv run pytest packages/sandbox -m docker   # needs a Docker daemon
docker run --rm -v "$PWD/policies:/policies:ro" \
  openpolicyagent/opa test /policies -v

# Full E1–E4 suite against the live stack (real repo + real LLM spend)
uv run pytest tests/e2e -m e2e -v
```

**LLM provider.** `LLM_API_KEY` is provider-agnostic; the provider is selected
by the `MODEL` prefix and resolved inside the llm-proxy. Compose hands the key
to LiteLLM under every name it might read (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`), so `MODEL=gpt-4o-mini` and
`MODEL=anthropic/claude-haiku-4-5` both work by changing only `MODEL` (and
the key).

**CI.** `.github/workflows/ci.yml` runs unit tests, `opa test`, image builds,
and a **secret-inspection gate** that fails if any `*_TOKEN`/`*_KEY` other
than `AGENT_BEARER_TOKEN` reaches the agent service env or image layers. The
E2E job is manual (`workflow_dispatch` with `run_e2e`) because it needs real
credentials and a disposable repo.
