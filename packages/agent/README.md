# agent

The brain host: the OpenHands Agent SDK running as a library behind Poseidon
contracts. This is the only package where an LLM is in the loop, and it is
built on the assumption that the model will sometimes try things it should
not — so it holds no credential, owns no side effect, and cannot reach
anything except through the gateway.

The SDK owns the reasoning loop, context machinery and coding heuristics.
Nothing else. Its built-in tools and its sub-agent machinery (`TaskToolSet`)
are never installed or registered; the only tools the model ever sees are
the Hands adapters below, and every one of them resolves to a gateway MCP
call.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The HTTP API: `POST /task` (202, runs in the background), `GET /task/{run_id}`. |
| `brain.py` | The five dispatch modes, the SDK conversation setup, and `GatewayBridge`. |
| `hands.py` | The OpenHands tool definitions — the model-facing schemas — each resolving to a gateway call. |
| `gateway_client.py` | The MCP client. |
| `config.py` | Settings. |
| `prompts/` | `system.md` and `pm.md`, edited as prose. |

## Five dispatch modes, four tool surfaces

| Mode | Tools | Ends at |
|---|---|---|
| `implement` | `list_dir`, `read_file`, `write_file`, `exec` | a completion claim |
| `deliver` | the above **+ `open_pr`** | the PR |
| `review` | read/exec/`run_tests` + `read_pr`, `comment_pr` | posted findings |
| `pm` | `create_issue`, `read_issues`, `request_crew` | tickets + recorded crew intent |
| `plan` | none — one completion, no tool loop | the task brief |

**Delivery before verification is structurally impossible.** The implement
surface has no `open_pr` at all; delivery is a *separate* dispatch the
workflow issues only after the platform verify gate is green. The model
cannot self-deliver by deciding it is finished, because the tool does not
exist in the conversation it is having.

The planner is LLM-only: it borrows the executor's brain host but calls the
gateway under its **own** minted token, so its compiled read-only scope is
what policy enforces.

## Two mechanisms worth knowing

- `run_id` and `target_repo` are injected at call time and appear in **no**
  action schema. The model cannot see or forge them.
- `open_pr` is a composite: `sandbox.git_bundle` then `git.open_pr`. The
  bundle's base64 never enters the model's context.

## Credential posture

The pod holds no secret. Its gateway credential is either a kubelet-rotated
projected service-account token file (standing hires) or a minted short-TTL
JWT injected as `GATEWAY_TOKEN` at pod creation (ephemeral crew members) —
env rather than file because it is minted per run and revoked by registry
row, never rotated. `GPG_KEY`, which the python base image bakes in, is
blanked so that no token-shaped value exists in a company-namespace pod at
all.

## Entry points

```
uvicorn --factory agent.app:create_app --host 0.0.0.0 --port 8000
```

`Dockerfile.agent`. `ROLE_TEMPLATE=pm` runs the PM loop; anything else runs
the engineer loop.

`POST /task` answers 409 for a run already in flight — that is crash-resume
re-attach, not an error. A *terminal* run re-dispatched is a restart, which
is what makes the budget-pause resume path work instead of polling a stale
failure forever.

## Cross-package interactions

- Every side effect goes to `gateway` over MCP.
- Model access goes to `gateway-llm`, which meters it and enforces the
  budget; a `budget_exhausted` 429 surfaces immediately rather than being
  retried.
- `orchestrator` dispatches into this API and polls it.
- `mockllm` is the deterministic brain the e2e suite runs against.

## Tests

```
uv run pytest packages/agent
```

The unit suite drives the **production** SDK loops with a scripted LLM and a
fake gateway that mimics real policy behaviour (feature-branch rule
included), so allow, deny and recovery paths are all exercised.
`test_bridge.py` is the exception: it runs against a real MCP
streamable-HTTP server, because the bridge plumbing is where the first
cluster failures showed up.

## Invariants

- The brain starts and stops no sandboxes and owns no process-level
  retries, timeouts or cancellation — the workflow dispatches and waits.
  `num_retries=1` keeps the SDK from grinding on errors the workflow owns.
- A policy denial comes back as a tool observation the model can recover
  from, never a crash.
- Prompt files are rendered with literal placeholder substitution, not
  `str.format`: they are prose, and a stray brace must stay a brace.
- The mode runners are synchronous (the SDK conversation loop blocks) and
  run in a worker thread under the async app; `GatewayBridge` gives them a
  sync facade over the async MCP client by parking the session on a
  dedicated event-loop thread.
