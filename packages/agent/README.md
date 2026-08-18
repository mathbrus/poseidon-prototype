# agent

The brain host: the OpenHands Agent SDK running as a library behind
Poseidon contracts. This is the only package where an LLM is in the loop,
and it is built on the assumption that the model will sometimes try things
it should not — so the pod holds no credential, owns no side effect, and
cannot reach anything except through the gateways.

If the [root README](../../README.md) is the argument that a compromised
agent must be survivable, this package is the thing being survived: the
process that turns untrusted text into tool calls. Nothing it decides is
trusted anywhere else.

## Why "brain host", not "agent framework"

The OpenHands SDK is adopted strictly as a library, for exactly one job:
the reasoning loop, the context machinery (a summarizing condenser), and
the coding heuristics. Nothing else crosses the boundary:

- Its built-in tools — bash, file editor, browser — are never installed,
  and its sub-agent machinery (`TaskToolSet`) is never registered. The
  SDK's own default tools are control flow only (`finish`/`think`).
- The `Conversation` is given a throwaway temp directory as its
  "workspace". The real workspace lives in the run's sandbox, on the far
  side of the gateway; the SDK never touches it directly.

The only tools the model ever sees are the Hands adapters below, and each
one resolves to a gateway MCP call. The same adopt-or-borrow discipline
governed the runtime side: the external sandbox runtime OpenShell was
evaluated and **not adopted** — its posture was borrowed and its backend
removed (see [sandbox](../sandbox/README.md), "On OpenShell"). Both
decisions follow one rule: third-party code may think for the agent, but
only Poseidon code may act for it.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The HTTP API (contract C6): `POST /task`, `GET /task/{run_id}`. |
| `brain.py` | The mode runners, the SDK conversation setup, `GatewayBridge`. |
| `hands.py` | The model-facing tool schemas, each resolving to a gateway call. |
| `gateway_client.py` | The async MCP client (`McpGateway`, `ToolCallError`). |
| `config.py` | Settings, including gateway-credential resolution. |
| `prompts/` | One file per dispatch mode (`pm.md`, `plan.md`, `implement.md`, `deliver.md`, `review.md`), edited as prose. |

## Dispatch: the /task API

The orchestrator's workflows drive this host through one small API.
`POST /task {run_id, task_text, mode?, gateway_token?}` answers 202 and
runs the dispatch on a background task; `GET /task/{run_id}` reports
`{status, pr_url, iterations, error, brief, ticket_urls,
crew_request_id, comment_urls}`. The workflow polls — the brain owns no
process-level retries, timeouts or cancellation, because Temporal already
owns all three.

`mode` selects the runner: `implement` (the default), `plan`, `deliver`,
`review`. A fifth loop hides behind the default: a pod deployed with
`ROLE_TEMPLATE=pm` routes default-mode dispatches to the PM runtime
instead of the engineer loop — the PM is a different standing instance of
the same image, not a different mode of the same agent.

Two status-code rules carry real semantics:

- **409 for a run already in flight.** Crash-resume re-attach: a
  workflow retry after a worker death finds its run still going and
  waits, instead of starting a duplicate. It is also the contract that
  one brain host runs one dispatch at a time.
- **A terminal run re-dispatched is a restart.** After a budget pause
  is lifted, the workflow re-dispatches the same `run_id`; if that
  answered 409 too, it would poll a stale failure forever.

## Four tool surfaces, five runners

From `hands.py`, verbatim:

```python
ACT_TOOLS = ["list_dir", "read_file", "write_file", "exec"]
DELIVER_TOOLS = ACT_TOOLS + ["open_pr"]
PM_TOOLS = ["create_issue", "read_issues", "request_crew"]
REVIEWER_TOOLS = ["list_dir", "read_file", "exec", "run_tests", "read_pr", "comment_pr"]
```

| Runner | Tools | Ends at |
|---|---|---|
| `implement` | `ACT_TOOLS` | a completion claim |
| `deliver` | `DELIVER_TOOLS` | the PR URL |
| `review` | `REVIEWER_TOOLS` | posted findings |
| `pm` | `PM_TOOLS` | tickets + recorded crew intent |
| `plan` | none — one completion, no tool loop | the task brief |

**Delivery before verification is structurally impossible.** The
implement surface has no `open_pr` at all. `implement_dispatch` seeds the
conversation with the task plus a workspace survey the host fetched
itself, then runs an *advisory* verify — its own `sandbox.run_tests`
call between conversation turns — feeding failures back until green or
`max_iterations`. Advisory green ends the dispatch with nothing but a
claim; the gate of record is the platform's own verify in the workflow.
Only after that is green does the workflow issue a separate
`mode="deliver"` dispatch whose conversation contains `open_pr`. The
model cannot self-deliver by deciding it is finished, because in the
conversation it is having, the tool does not exist. (A model that calls
`open_pr` anyway gets the SDK's tool-not-found feedback; the gateway is
never contacted.)

The reviewer's surface is read-and-judge: it reads the PR and executes
the suite in its review sandbox, but has no `write_file` and no
`open_pr` — and the compiled sdet scope enforces the same at the gateway,
so the in-process surface is a convenience, not the boundary.

Every runner fails on missing evidence rather than on the model's word: a
deliver dispatch with no PR opened, a review with no findings posted, a
PM dispatch with no ticket filed — each is an explicit error.

## Hands: schemas the model cannot abuse

`hands.py` is where the model-facing contract and the platform contract
meet, and they are deliberately not the same. The schema the model sees:

```python
class ExecAction(Action):
    cmd: str = Field(description="Shell command line.")
    timeout_s: float = Field(default=60, description="Timeout in seconds (default 60).")
```

and the gateway call it resolves to:

```python
def _exec(ctx: HandsContext, a: ExecAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "sandbox.exec",
        {"run_id": ctx.run_id, "cmd": a.cmd, "timeout_s": a.timeout_s},
    )
```

`run_id` and `target_repo` appear in **no** action schema; they are
injected here, from the dispatch's `HandsContext`. The model cannot see
or forge what it cannot name. And because this injection runs inside the
agent's own pod — exactly the process assumed compromised — the gateway
does not trust it either: it re-verifies the claimed `run_id` against the
caller's registry binding on every call
([gateway](../gateway/README.md)). The injection is hygiene; the binding
check is the enforcement.

Three more mechanisms worth knowing:

- **`open_pr` is a composite**: `sandbox.git_bundle`, then `git.open_pr`
  with the bundle attached. The observation returned to the model is just
  the PR URL — base64 bundles never enter the model context (a test
  asserts no message the model saw contains the bundle bytes).
- **A denial is a tool result, not a crash.** A gateway deny raises
  `ToolCallError` carrying the OPA rule name; the executor turns it into
  an `ERROR: ...` observation. The model reads the rule name and can
  recover — the tests script an agent that tries `open_pr` on `main`,
  sees `git_open_pr_protected_branch`, and retries on `feature/*`.
- **Results are recorded as they happen.** `ToolRecords` captures the PR
  URL, ticket URLs, crew-request id and comment URLs at the executor, so
  the dispatch reports facts it saw, never a parse of the transcript.

One implementation note: the SDK resolves tools lazily, on a worker
thread, at first `send_message` — so the per-dispatch context is a
module-level current-context bound by `HandsContext.activate()`, not a
contextvar. That is safe only because the 409 rule means one dispatch per
host at a time; there is never a second context to race with.

## The planner's pod is the cheapest one

`mode="plan"` is the one dispatch that is LLM-only: the host surveys the
sandbox root (one `sandbox.list_dir` call), asks for a single completion
— no tool loop, no writes — and returns the resulting task brief, which
the executor's next dispatch carries verbatim.

The planner runs on its own pod, under its own per-run ServiceAccount —
one principal, one pod, one projected token, like every other agent. No
credential rides the dispatch; both the survey call and the LLM call run
under the pod's own token, so what policy enforces is the planner's
compiled read-only scope.

## Credential posture

The pod holds no secret. Its gateway credential has exactly one shape
(`config.py`, `resolve_gateway_token`): a kubelet-rotated projected
service-account token *file*, scoped to the gateway's audience — for
standing hires and ephemeral crew members alike. It is read fresh at
each dispatch, so a task never outlives the freshly read token's
validity window; a crew member's revocation is a registry-row
termination the gateway checks per call, not a property of the token.

The credential is good for exactly one thing: authenticating
to the Poseidon gateways. The LLM provider key lives in the llm-proxy,
the git token in the MCP gateway. The pod specs that deploy this image —
the operator for standing hires, the orchestrator for crew pods — also
blank `GPG_KEY`, which the python base image bakes in, so no token-shaped
env value exists in a company-namespace pod at all; the images CI job
asserts no secret-shaped env is baked in.

## GatewayBridge: sync brain, async client

The SDK conversation loop is synchronous and blocking, so each dispatch
runs in a worker thread under the async app (`asyncio.to_thread`), and
`GatewayBridge` gives the tool executors a sync facade over the async MCP
client. The subtlety it exists for: the MCP client's anyio cancel scopes
are task-bound, so the session must be entered and exited inside one
long-lived task on a dedicated event-loop thread — entering and exiting
it from separately scheduled coroutines corrupts the scopes on teardown.
This is where the first cluster run of the brain host died, which is why
`test_bridge.py` runs against a real MCP streamable-HTTP server rather
than a double. The per-call read timeout is 660 s at the MCP client, with
a 700 s backstop at the bridge.

## Entry points

```
uvicorn --factory agent.app:create_app --host 0.0.0.0 --port 8000
```

`Dockerfile.agent` (repo root) is the image both standing hires and crew
pods run; the pod spec supplies `MCP_GATEWAY_URL`, `LLM_PROXY_URL`,
`MODEL`, `TARGET_REPO`, `ROLE_TEMPLATE`, `MAX_ITERATIONS`, and one of
`GATEWAY_TOKEN_FILE` / `GATEWAY_TOKEN`.

## Cross-package interactions

- Every side effect goes to [`gateway`](../gateway/README.md) over MCP.
- Model access goes to [`gateway-llm`](../gateway-llm/README.md),
  authenticated with the same workload credential and attributed to the
  run via the `X-Poseidon-Run-Id` header. `num_retries=1` keeps the SDK
  from grinding on errors the workflow owns: a `budget_exhausted` 429
  surfaces after a single attempt, which is what the workflow's pause
  keys on.
- [`orchestrator`](../orchestrator/README.md) dispatches into `/task` and
  polls it.
- [`mockllm`](../mockllm/README.md) is the deterministic brain the e2e
  suite runs against; nothing in this package knows it exists.

## Tests

```
uv run pytest packages/agent
```

28 tests. The unit suite drives the **production** SDK loops: only the
two transports are replaced — a scripted litellm patch stands in for the
model, and a fake gateway behind a sync fake bridge mimics real policy
behaviour, feature-branch rule included — so allow, deny and recovery
paths all run through the real conversation machinery. `test_bridge.py`
is the exception described above: a real MCP server, so that session
lifecycle bugs fail here, not on kind.

## Invariants

- The brain starts and stops no sandboxes and owns no process-level
  retries, timeouts or cancellation — the workflow dispatches and waits.
- The implement surface never contains `open_pr`; delivery is a separate
  workflow-directed dispatch after the platform verify gate.
- A policy denial comes back as a tool observation the model can recover
  from, never a crash.
- `run_id` and `target_repo` are injected at call time and appear in no
  model-facing schema.
- Prompt files are rendered with literal placeholder substitution, not
  `str.format`: they are prose, and a stray brace must stay a brace.
- One dispatch at a time per brain host (409 on re-dispatch); a terminal
  run re-dispatched restarts.
