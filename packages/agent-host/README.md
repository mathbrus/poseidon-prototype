# agent-host

The agent host is the process inside every agent pod: a dispatch HTTP API in front of the OpenHands Agent SDK, used as a library. The orchestrator's workflows dispatch procedures to it, and it runs in standing and ephemeral agent pods in the company namespace.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-agent-host` | `uvicorn --factory agent_host.app:create_app --host 0.0.0.0 --port 8000` | nothing (a projected ServiceAccount token file, read per dispatch) | nothing |

## Module map

| Module | Owns |
|---|---|
| `app.py` | The dispatch API: `POST /task`, `GET /task/{run_id}`, the role check and the 409 rule. |
| `procedures.py` | The five procedures, `ProcedureResult`, `GatewayBridge`, `OrderedReadsExecutor`, the step caps. |
| `hands.py` | The model-facing tool schemas and the per-procedure tool surfaces; each tool resolves to a gateway call. |
| `prompting.py` | The system prompt (procedure file, vision, bounds, SDK heuristic sections) and the untrusted context. |
| `models.py` | The model table: wire shape (chat or Responses) and reasoning knob per model family. |
| `gateway_client.py` | The async MCP client (`McpGateway`) and `ToolCallError`. |
| `config.py` | `AgentSettings`, read from the pod env. |
| `prompts/` | One prose file per procedure. Each opens with `# Poseidon procedure: <name>` and carries `{bounds}`. |

The pod env sets `MCP_GATEWAY_URL`, `LLM_PROXY_URL`, `MODEL`, `ROLE`, `GATEWAY_TOKEN_FILE`, `TARGET_REPO` and `VISION`, with no defaults.

## How it works

### The dispatch API runs one procedure at a time

`POST /task {run_id, procedure, task_text, bounds, documents}` answers 202 and runs the procedure on a worker thread. `GET /task/{run_id}` answers `status` (`running`, `done`, `failed`) plus every field of `ProcedureResult`, so a new result field always crosses the boundary. The workflow polls. Temporal owns retries, timeouts and cancellation; the host owns none.

- **422** when the pod's `ROLE` does not declare the procedure in the catalog, or when `bounds` is missing. No model call happens.
- **409** when the same `run_id` is still running. A workflow retry after a worker crash re-attaches instead of starting a duplicate.
- **202** when a finished `run_id` is dispatched again. This is a restart, used after a budget park is lifted.
- **404** from `GET` for an unknown run.

### Each procedure has its own tool surface

| Procedure | Tools | Ends at |
|---|---|---|
| `scope` | `create_issue`, `read_issues`, `comment_issue` | tickets filed and the PM's summary commented; filing nothing is valid |
| `decide` | none: one completion, no gateway session | a `DECISION:` line and a `RATIONALE:` line |
| `implement` | `read_file`, `write_file`, `edit_file`, `exec`, `request_architecture` | a completion claim after advisory green |
| `deliver` | `read_file`, `open_pr` | the PR URL, or the refusal that kept it from opening |
| `review` | `read_file`, `exec`, `run_tests`, `read_pr`, `comment_pr`, `request_architecture`, `verdict` | a findings comment and a pass/fail verdict |

The implement surface has no `open_pr`. Between conversation turns, `run_implement` calls `sandbox.run_tests` itself and feeds failures back, up to `VERIFY_CYCLES`. This advisory verify uses `common.verify`, the same rule as the platform's verify. The platform's own verify in the workflow is the gate of record. Only after it passes does the workflow dispatch `deliver`, whose surface holds `open_pr` and nothing that changes the sandbox: `sandbox.git_bundle` makes the delivery commit of the workspace as it stands, so what is delivered is what was verified. The deliver dispatch opens on what changed, read by the host through `sandbox.exec`. When policy refuses the work itself, the refusal is the result's `delivery_refusal`, and the workflow hands it to the next implement dispatch.

Results are facts captured at the tool executor (`ToolRecords`), never the model's prose. A deliver with no PR, a review with no findings or verdict, a scope that filed tickets without a summary, and a malformed decide reply all fail. So does a loop that hits the SDK's step cap or stuck detector.

### Every side effect goes through the gateway

The SDK's built-in tools are never installed. Each tool resolves to an MCP call on the [mcp-gateway](../mcp-gateway/README.md), except `verdict`, which only records the answer. `hands.py` injects `run_id` and `target_repo`, which appear in no schema. `open_pr` has the platform commit and bundle the workspace, then opens the PR, and the model sees only the URL. A denial becomes an `ERROR: ...` observation naming the rule, so the model can recover.

Before `implement` and `review` start, the host lists the sandbox's files (`sandbox.list_dir`, recursive and .gitignore-aware) into the opening message and reads `AGENTS.md`. That file and the company's documents reach the model inside the SDK's untrusted-content framing. The model has no listing tool of its own; it searches with `rg` or `git grep` through `exec`.

`GatewayBridge` opens and closes the MCP session in one task on its own thread, because the MCP client's cancel scopes are task-bound. Model calls go to the [llm-gateway](../llm-gateway/README.md) with `X-Poseidon-Run-Id` and `num_retries=1`, so a budget 429 surfaces at once. A batch of tool calls runs concurrently only when every call is read-only.

## Invariants

- A role runs only the procedures its catalog entry declares: `test_app.py::test_a_role_runs_only_its_own_procedures`.
- One run per `run_id` at a time; a finished run restarts: `test_app.py::test_duplicate_run_id_is_409`, `test_terminal_run_restarts_on_redispatch`.
- The implement surface never holds `open_pr`: `test_procedures.py::test_prompt_and_toolsets_per_phase`.
- A policy denial is a tool observation, never a crash: `test_procedures.py::test_denied_pr_is_surfaced_and_agent_retries_on_feature_branch`, `test_pm.py::test_pm_deny_surfaces_as_tool_error_not_crash`.
- Prompt files use literal substitution: `test_procedures.py::test_prompt_rendering_survives_stray_braces`.
- Every prompt opens with its own header: `test_procedures.py::test_every_prompt_opens_with_its_own_header`.
- `AGENTS.md` reaches the model only as untrusted context: `test_procedures.py::test_agents_md_rides_the_system_message_as_untrusted_context`.
- A stuck or capped loop is a named failure: `test_procedures.py::test_repeating_itself_ends_the_dispatch_as_stuck`.
- Only all-read batches run concurrently: `test_procedures.py::test_a_batch_with_writes_runs_in_the_models_order`.

## Testing

```bash
uv run pytest packages/agent-host
```

See [Contributing](../../docs/developing/contributing.md). No cluster is needed. The suite drives the production SDK loops with a scripted model and a fake gateway. `test_bridge.py` uses a real MCP server on localhost. The [acceptance suite](../../docs/developing/acceptance.md) runs the host on kind.

## Related

- [How it works](../../docs/explanation/how-it-works.md), [Security model](../../docs/explanation/security-model.md)
- [Tools](../../docs/reference/tools.md), [Roles and crews](../../docs/reference/roles.md), [Use a paid brain](../../docs/how-to/paid-brain.md)
- [orchestrator](../orchestrator/README.md), [mock-brain](../mock-brain/README.md)
