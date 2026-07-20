# Poseidon PoC

One coding agent ships one pull request under policy: every side effect is
policy-checked, credential-free from the agent's perspective, and audited.

See [markdowns/prototype.md](markdowns/prototype.md) for the full specification.

## Running the stack (Docker Compose)

The runtime lives in `deploy/compose/`. The network topology is the egress
enforcement mechanism: `agentnet`, `sandboxnet` and `opanet` are internal
(no route out); only the gateway and the LLM proxy sit on the `egress` bridge.

```bash
cd deploy/compose
cp .env.example .env     # fill in GIT_TOKEN, LLM_API_KEY, TARGET_REPO, MODEL
docker compose up -d --build
docker compose ps        # all four services report healthy
```

Services: `opa` (policies from `policies/` mounted read-only), `llm-proxy`
(stock LiteLLM, holds `LLM_API_KEY`), `mcp-gateway` (holds `GIT_TOKEN`,
audit log bind-mounted to `runs/`), `agent` (only `agentnet`; the sole
secret-shaped variable in its environment is `AGENT_BEARER_TOKEN` — verify
with `docker compose exec agent env | grep -Ei 'token|key'`).

The host-side runner reaches the gateway on `http://127.0.0.1:8080`
(published port) and the agent on `http://172.28.10.10:8000` (fixed address
on the internal `agentnet`, routable from the host bridge; internal networks
cannot publish ports). Per-run sandbox containers are attached to
`poseidon-sandboxnet` by the runner — `.env` carries it as `SANDBOX_NETWORK`,
along with `AGENT_URL` and the host-view `MCP_GATEWAY_URL`.

## Sandbox image

The sandbox container image (toolchain prebaked, since sandboxes have zero egress)
is built from the repo root:

```bash
docker build -f Dockerfile.sandbox -t poseidon-sandbox .
```

Sandbox containers are provisioned per run by the Docker backend
(`sandbox.backends.docker.DockerBackend`). Its integration tests need a running
Docker daemon and are excluded from plain `pytest` runs; run them with:

```bash
uv run pytest packages/sandbox -m docker
```

## MCP Gateway

The gateway (`packages/gateway`) is the single enforcement point: an MCP server
(streamable HTTP, bearer auth) exposing the `sandbox.*` tools, which are
policy-checked against OPA and audited to `runs/audit.jsonl` before being proxied
to the sandbox registered for the run. Sandboxes are registered by the runner via
`POST /admin/sandboxes {run_id, url}`.

It also carries the two git tools — the only paths by which code enters or leaves
the system: `repo.archive` (runner-only; clones the target repo with `GIT_TOKEN`,
strips remotes/credentials, returns a base64 workspace tarball) and `git.open_pr`
(verifies the agent's git bundle, applies it to a platform-side clone, feeds the
changed paths to policy, then pushes the branch and opens the PR via the GitHub
API). The gateway image is built from the repo root:

```bash
docker build -f Dockerfile.gateway -t poseidon-gateway .
```

Run it standalone (Compose wiring arrives in a later step):

```bash
AGENT_BEARER_TOKEN=... RUNNER_BEARER_TOKEN=... GIT_TOKEN=... \
uv run uvicorn --factory gateway.app:create_app
```

Besides the shared env vars it needs (`AGENT_BEARER_TOKEN`, `RUNNER_BEARER_TOKEN`,
`GIT_TOKEN`, `BASE_REF`), it reads `OPA_URL` (default `http://opa:8181`),
`AUDIT_PATH` (default `runs/audit.jsonl`) and `GITHUB_API_URL`
(default `https://api.github.com`).

## Agent

The backend-engineer agent (`packages/agent`) is a FastAPI service running a
LangGraph plan → act → verify → deliver loop. It accepts work via
`POST /task {run_id, task_text}` (202, runs in the background) and reports via
`GET /task/{run_id}` → `{status, pr_url, iterations, error}`. All side effects
go through the gateway's MCP tools (bearer-authed with `AGENT_BEARER_TOKEN`);
the LLM is reached through the proxy at `LLM_PROXY_URL`. Policy denials are fed
back to the model so it can recover (e.g. move the work onto a `feature/*`
branch). The image is built from the repo root:

```bash
docker build -f Dockerfile.agent -t poseidon-agent .
```

It needs only non-secret config plus its own bearer token: `MCP_GATEWAY_URL`,
`LLM_PROXY_URL`, `MODEL`, `TARGET_REPO`, `MAX_ITERATIONS` (default 10) and
`AGENT_BEARER_TOKEN`.

## Runner

The runner CLI (`packages/runner`) owns the implement flow and the sandbox
lifecycle: `create_run → fetch_workspace → provision_sandbox → dispatch_task →
teardown`, with per-step timeouts and a guaranteed sandbox destroy on any
failure. Each run writes `runs/<run_id>/manifest.json` (task, timestamps,
status, PR URL, iterations, audit-line count).

```bash
uv run poseidon-poc run examples/task-happy.md   # execute a task end to end
uv run poseidon-poc status <run_id>              # print a run's manifest
```

It needs `RUNNER_BEARER_TOKEN`, `MCP_GATEWAY_URL`, `TARGET_REPO` and `BASE_REF`,
plus runner-local knobs (all with defaults): `AGENT_URL`
(`http://localhost:8000`), `SANDBOX_IMAGE` (`poseidon-sandbox`),
`SANDBOX_NETWORK` (`sandboxnet` — set this to the compose project's sandbox
network name), `RUNS_DIR` (`runs`), `STEP_TIMEOUT_S`, `AGENT_TIMEOUT_S`.
A running Docker daemon is required to provision sandboxes.

## Policies

OPA/Rego policies live in `policies/`. Run their tests with a local `opa` binary or
via Docker:

```bash
docker run --rm -v "$PWD/policies:/policies:ro" openpolicyagent/opa test /policies -v
```
