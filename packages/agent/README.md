# agent

The agent host: the dispatch HTTP API and the OpenHands Agent SDK
running as a library behind Poseidon contracts. This is the only package
where an LLM is in the loop, and it is built on the assumption that the
model will sometimes try things it should not — so the pod holds no
credential, owns no side effect, and cannot reach anything except through
the gateways.

If the [root README](../../README.md) is the argument that a compromised
agent must be survivable, this package is the thing being survived: the
process that turns untrusted text into tool calls. Nothing it decides is
trusted anywhere else.

## Why "agent host", not "agent framework"

The OpenHands SDK is adopted strictly as a library: the reasoning loop,
the context machinery (a summarizing condenser), the stuck detector, the
batch executor for tool calls, and the prompt sections that carry its
coding heuristics. Nothing else crosses the boundary:

- Its built-in tools — bash, file editor, browser — are never installed,
  and its sub-agent machinery (`TaskToolSet`) is never registered. The
  SDK's own default tools are control flow only (`finish`/`think`).
- The `Conversation` is given a throwaway temp directory as its
  "workspace". The real workspace lives in the run's sandbox, on the far
  side of the gateway; the SDK never touches it directly. Its loaders
  for project skills and memory stay off for the same reason — they read
  the pod's disk, which holds nothing.
- Its default system prompt is not used either. It addresses a user in
  the loop, an OpenHands identity, a networked machine, and a writable
  `AGENTS.md`; none exist here. `prompting.py` composes each procedure's
  prompt as Poseidon's own procedure followed by the SDK sections that
  say nothing about who the model is or which tools it holds
  (efficiency, file hygiene, code quality, per-model-family guidance),
  rendered by the SDK's section classes so a version bump updates the
  text.

The only tools the model ever sees are the Hands adapters below, and each
one resolves to a gateway MCP call. One rule governs the boundary:
third-party code may think for the agent, but only Poseidon code may act
for it.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The HTTP API: `POST /task`, `GET /task/{run_id}`. |
| `procedures.py` | The five procedures (`run_scope`, `run_decide`, `run_implement`, `run_deliver`, `run_review`), the SDK conversation setup, `GatewayBridge`. |
| `hands.py` | The model-facing tool schemas, each resolving to a gateway call. |
| `prompting.py` | Everything that reaches the model before its first turn: the procedure's text followed by the SDK's heuristic sections, and the repository's `AGENTS.md` as untrusted context. |
| `gateway_client.py` | The async MCP client (`McpGateway`, `ToolCallError`). |
| `config.py` | Settings, including the pod's role and gateway-credential resolution. |
| `prompts/` | One file per procedure (`scope.md`, `decide.md`, `implement.md`, `deliver.md`, `review.md`), edited as prose. Each opens with the one line that is a contract with the mock brain: `# Poseidon procedure: <name>` (`PROCEDURE_HEADER`), which the host also checks against the file's name. |

## Dispatch: the /task API

The orchestrator's workflows drive this host through one small API.
`POST /task {run_id, procedure, task_text, documents}` answers 202 and
runs the procedure on a background task; `GET /task/{run_id}` reports
`status` plus every field of the `ProcedureResult` (`pr_url`,
`iterations`, `error`, `ticket_urls`, `comment_urls`, `verdict`,
`verdict_rationale`, `decision`) — the HTTP shape is derived
from the result type, so a new field cannot be dropped at the
boundary. The workflow polls — the host owns no
process-level retries, timeouts or cancellation, because Temporal already
owns all three.

`procedure` names one of five: `scope`, `decide`, `implement`, `deliver`,
`review`. The pod's `ROLE` bounds which of them it runs — the catalog
declares `pm: scope, decide`, the coding roles `implement, deliver`, the
reviewer `review` — and a dispatch outside that set is refused with 422
before any model call. That is defence in depth under the gateway's
compiled tool policy, not a substitute for it. The scope procedure ends
only once its one-paragraph summary is a comment on every ticket it
filed: a ticket without one is an unfinished dispatch, reported as such
with the tickets still linked.

Two status-code rules carry real semantics:

- **409 for a run already in flight.** Crash-resume re-attach: a
  workflow retry after a worker death finds its run still going and
  waits, instead of starting a duplicate. It is also the contract that
  one agent host runs one dispatch at a time.
- **A terminal run re-dispatched is a restart.** After a budget pause
  is lifted, the workflow re-dispatches the same `run_id`; if that
  answered 409 too, it would poll a stale failure forever.

## Five procedures, four tool surfaces

From `hands.py`, verbatim:

```python
IMPLEMENT_TOOLS = [
    "list_dir",
    "read_file",
    "write_file",
    "edit_file",
    "exec",
    "request_architecture",
]
DELIVER_TOOLS = ["list_dir", "read_file", "write_file", "edit_file", "exec", "open_pr"]
SCOPE_TOOLS = ["create_issue", "read_issues", "comment_issue"]
REVIEW_TOOLS = [
    "list_dir",
    "read_file",
    "exec",
    "run_tests",
    "read_pr",
    "comment_pr",
    "verdict",
]
```

| Procedure | Tools | Ends at |
|---|---|---|
| `scope` | `SCOPE_TOOLS` | tickets + recorded crew intent + the PM's summary, commented on each ticket |
| `decide` | none — one completion, no tool loop | one action per heartbeat tick, with its rationale |
| `implement` | `IMPLEMENT_TOOLS` | a completion claim |
| `deliver` | `DELIVER_TOOLS` | the PR URL |
| `review` | `REVIEW_TOOLS` | posted findings + a structured pass/fail verdict |

Which catalog role may be asked to run which procedure is the catalog's
to say, rendered from `schema.catalog` on
[Roles and crews](../../docs/reference/roles.md).

`edit_file` is the partial edit coding models are trained on —
`str_replace` of text that occurs exactly once, or `insert` after a line —
and `read_file` answers with numbered lines (optionally a line range) so
an edit has something to anchor on. Both resolve to run-scoped gateway
tools like every other sandbox call.

**Delivery before verification is structurally impossible.** The
implement surface has no `open_pr` at all. `run_implement` seeds the
conversation with the task plus the workspace survey (below), then runs
an *advisory* verify — its own `sandbox.run_tests`
call between conversation turns — feeding failures back until green or
`VERIFY_CYCLES` (a cap of the host, beside the per-procedure step caps).
Green is `common.verify`'s word, the same the gate of record uses, so a
tree with no tests ends the loop instead of pushing the model to invent
one.
Advisory green ends the dispatch with nothing but a
claim; the gate of record is the platform's own verify in the workflow.
Only after that is green does the workflow issue a separate `deliver`
dispatch whose conversation contains `open_pr`. The
model cannot self-deliver by deciding it is finished, because in the
conversation it is having, the tool does not exist. (A model that calls
`open_pr` anyway gets the SDK's tool-not-found feedback; the gateway is
never contacted.)

The reviewer's surface is read-and-judge: it reads the PR and executes
the suite in its review sandbox, but has no `write_file` and no
`open_pr` — and the compiled reviewer scope enforces the same at the gateway,
so the in-process surface is a convenience, not the boundary. Its step
budget is `REVIEW_STEP_CAP`, one of the per-procedure caps beside
`SCOPE_STEP_CAP`, `IMPLEMENT_STEP_CAP` and `DELIVER_STEP_CAP`:
`VERIFY_CYCLES` bounds only how many advisory verify cycles an implement
dispatch gets, so it can never starve another procedure.

Every procedure fails on missing evidence rather than on the model's word: a
deliver dispatch with no PR opened, a review with no findings posted, a
PM dispatch with no ticket filed — each is an
explicit error. So is a loop that stops short: the SDK sets `ERROR` at
the step cap and `STUCK` when its detector sees the same action and
observation repeat, and `_loop_failure` turns either into a named
failure in every procedure. The detector is on for every model — the host
does not know which brain is behind the proxy, and should not.

## The workspace survey

Every sandboxed dispatch — implement, deliver, review — opens with
a survey the host runs itself, under the pod's own token: one
`sandbox.list_dir` of the root, and a `sandbox.read_file` of the
repository's `AGENTS.md` when there is one (a role policy will not let
read simply gets no instructions). The listing rides the first user
message. The instructions are handed to the SDK as a repo skill, which
renders them in the system message's second content block inside the
SDK's `UNTRUSTED_CONTENT` framing — repository text is exactly that —
while the static prompt stays byte-identical across dispatches.

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
([gateway](../mcp-gateway/README.md)). The injection is hygiene; the binding
check is the enforcement.

Three more mechanisms worth knowing:

- **`open_pr` is a composite**: `sandbox.git_bundle`, then `git.open_pr`
  with the bundle attached. The observation returned to the model is just
  the PR URL — base64 bundles never enter the model context (a test
  asserts no message the model saw contains the bundle bytes).
- **A denial is a tool result, not a crash.** A gateway deny raises
  `ToolCallError` carrying the OPA rule name; the tool executor turns it into
  an `ERROR: ...` observation. The model reads the rule name and can
  recover — the tests script an agent that tries `open_pr` on `main`,
  sees `git_open_pr_protected_branch`, and retries on `feature/*`.
- **Results are recorded as they happen.** `ToolRecords` captures the PR
  URL, ticket URLs, crew-request id and comment URLs at the tool executor, so
  the dispatch reports facts it saw, never a parse of the transcript.

One implementation note: the SDK resolves tools lazily, on a worker
thread, at first `send_message` — so the per-dispatch context is a
module-level current-context bound by `HandsContext.activate()`, not a
contextvar. That is safe only because the 409 rule means one dispatch per
host at a time; there is never a second context to race with.

## Credential posture

The pod holds no secret. Its gateway credential has exactly one shape
(`config.py`, `resolve_gateway_token`): a kubelet-rotated projected
service-account token *file*, scoped to the gateway's audience — for
standing agents and ephemeral agents alike. It is read fresh at
each dispatch, so a task never outlives the freshly read token's
validity window; an ephemeral agent's revocation is a registry-row
termination the gateway checks per call, not a property of the token.

The credential is good for exactly one thing: authenticating
to the Poseidon gateways. The LLM provider key lives in the llm-proxy,
the git token in the MCP gateway. The pod specs that deploy this image —
the operator for standing agents, the orchestrator for ephemeral pods — also
blank `GPG_KEY`, which the python base image bakes in, so no token-shaped
env value exists in a company-namespace pod at all; the images CI job
asserts no secret-shaped env is baked in.

## GatewayBridge: sync loop, async client

The SDK conversation loop is synchronous and blocking, so each dispatch
runs in a worker thread under the async app (`asyncio.to_thread`), and
`GatewayBridge` gives the tool executors a sync facade over the async MCP
client. The subtlety it exists for: the MCP client's anyio cancel scopes
are task-bound, so the session must be entered and exited inside one
long-lived task on a dedicated event-loop thread — entering and exiting
it from separately scheduled coroutines corrupts the scopes on teardown.
This is where the first cluster run of the agent host died, which is why
`test_bridge.py` runs against a real MCP streamable-HTTP server rather
than a double. The per-call read timeout is 660 s at the MCP client, with
a 700 s backstop at the bridge.

Several tool threads may be in `call` at once. The four read tools
declare themselves read-only and the agent allows four calls in flight,
so a reply that reads three files runs the three reads concurrently; the
session multiplexes them, one coroutine each. `OrderedReadsExecutor` adds
the one rule the SDK's executor lacks: a batch fans out only when every
call in it is a read — a batch with a write, a command or a delivery
runs one call at a time in the model's order, because a commit must not
land before its checkout.

## Entry points

```
uvicorn --factory agent.app:create_app --host 0.0.0.0 --port 8000
```

`poseidon-agent` (the root `Dockerfile`, built by `deploy/build-images.sh`) is the image both standing agents and ephemeral
agents run; the pod spec supplies `MCP_GATEWAY_URL`, `LLM_PROXY_URL`,
`MODEL`, `TARGET_REPO` and `VISION` (the company's repository and
vision, from its applied blueprint), `ROLE` (the catalog role, which
bounds the procedures the host accepts) and `GATEWAY_TOKEN_FILE`. Every
loop bound — the per-procedure step caps and implement's verify cycles —
is a constant of the host, not a knob of the deployment.

## Cross-package interactions

- Every side effect goes to [`gateway`](../mcp-gateway/README.md) over MCP.
- Model access goes to [`llm-gateway`](../llm-gateway/README.md),
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

The unit suite drives the **production** SDK loops: only the
two transports are replaced — a scripted litellm patch stands in for the
model, and a fake gateway behind a sync fake bridge mimics real policy
behaviour, feature-branch rule included — so allow, deny and recovery
paths all run through the real conversation machinery. `test_bridge.py`
is the exception described above: a real MCP server, so that session
lifecycle bugs fail here, not on kind.

## Invariants

- The host starts and stops no sandboxes and owns no process-level
  retries, timeouts or cancellation — the workflow dispatches and waits.
- The implement surface never contains `open_pr`; delivery is a separate
  workflow-directed dispatch after the platform verify gate.
- A policy denial comes back as a tool observation the model can recover
  from, never a crash.
- `run_id` and `target_repo` are injected at call time and appear in no
  model-facing schema.
- Prompt files are rendered with literal placeholder substitution, not
  `str.format`: they are prose, and a stray brace must stay a brace.
- One dispatch at a time per agent host (409 on re-dispatch); a terminal
  run re-dispatched restarts.
- A loop that stops short — step cap or stuck — is a named failure,
  never a completion claim.
- Only reads run concurrently; a batch holding any write runs in the
  model's order.
- Repository text (`AGENTS.md`) reaches the model only inside the SDK's
  untrusted-content framing, never in the static prompt.
