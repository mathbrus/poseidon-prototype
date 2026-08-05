# Step 21 — Brain framework spike: O-2 and O-3 decided

Date: 2026-08-05. Scope: plan §6 step 21 — evaluate the OpenHands Agent SDK
(plus one peer) against the O-2 criteria, decide O-3, and sketch the Hands
adapter that step 22 will build. This document is the decision record; the
plan's §10 table and mvp-overview §4.3/§12 point here.

## Verdicts

- **O-2 — brain framework: the OpenHands Agent SDK** (`openhands-sdk` on
  PyPI, repo `github.com/OpenHands/software-agent-sdk`, MIT), adopted as a
  library behind Poseidon contracts, **version-pinned exactly** (spiked
  against 1.40.0/1.40.1). The peer, Anthropic's Claude Agent SDK, is
  rejected for this role (reasons below).
- **O-3 — Hands tool-surface width: option A** — the adapter maps the brain
  onto the **existing six `sandbox.*` tools** (plus the existing `open_pr`
  composite). No richer toolserver primitives at MVP; the escalation path is
  documented below and re-evaluated only if the step-22 parity suite shows
  fidelity loss.

## How the spike ran

Two tracks, both evidence-first:

1. **Hands-on**: `openhands-sdk` 1.40.0 installed in a scratch venv; the
   public API inspected (Agent/Conversation/LLM/tool registration/MCP
   config), then a **headless end-to-end run** executed: one custom
   gateway-stub tool registered, all built-in side-effect tools absent, the
   LLM scripted by patching the SDK's litellm call site (no provider, no
   cost). The loop dispatched the custom tool with the model-chosen
   arguments, the finish tool ended the run, `execution_status` reported
   `FINISHED`. The script is in the appendix.
2. **Survey**: current docs/repos for both SDKs plus the peer field
   (mini-swe-agent, smolagents, LangGraph/Open SWE), summarized below.

## O-2 scorecard (criteria from overview §12)

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| Tools cleanly resolvable to gateway MCP | **Pass** | The default tool surface is `['FinishTool', 'ThinkTool']` — pure control flow, zero side effects. Built-ins (bash, file editor, browser) live in a separate `openhands-tools` package and exist on an agent only if explicitly listed. Custom tools: `register_tool()` + `Agent(tools=[Tool(name=...)])` with an author-defined `ToolDefinition` (action/observation schemas + executor) — proven live in the headless run. Native MCP is also first-class (`mcp_config` per agent, bearer/header auth credentials, `filter_tools_regex`), but the adapter deliberately wraps the gateway client in custom ToolDefinitions instead (see sketch) to keep `run_id` injection and bundle composition. |
| No mandatory ownership of workspace/session lifecycle | **Pass** | `Conversation(workspace=...)` takes a plain local directory; with no file/bash tools registered it is inert scratch space — the SDK spawned nothing and touched nothing in the headless run. `DockerWorkspace`/`RemoteWorkspace` are optional flavors, not defaults. The host owns the loop: `run()`, `pause()`, `interrupt()`, `max_iteration_per_run`, stuck detection, status via `state.execution_status` — which is exactly the shape `dispatch_executor`'s poll loop needs. |
| Context machinery usable headless | **Pass** | `LLMSummarizingCondenser` is a plain field on `Agent` (`max_size`, `keep_first`, …), instantiated library-side with any `LLM`. Skills load via `load_skills_from_dir()`/`AgentContext` with no GUI anywhere. Confirmation policies and the LLM security analyzer are SDK-level too (unused by us — Law stays OPA at the gateway; at most defense-in-depth later). |
| License and pace of change | **Pass with a pin** | MIT. Cadence is fast: 80 releases from 1.0.0a1 (2025-10-14) to 1.40.1 (2026-08-05), ~6–15 per month, no formal semver contract found. Mitigation: pin the exact version in `packages/agent`, upgrade deliberately with the parity suite as the gate. Backing: the OpenHands company (All Hands AI lineage); the SDK is the engine under their CLI/Cloud, so it is load-bearing for them, not a side artifact. |

Two hygiene findings that carry into the adapter as requirements:

- **Budget-stop vs. retry**: `RateLimitError` (HTTP 429) is in the SDK's
  default LLM retry set (`num_retries=5`, 8–64 s exponential backoff). Our
  llm-gateway signals `budget_exhausted` as a 429 — the default would grind
  ~3 minutes of retries into a hard budget stop. The adapter must set
  `num_retries` low and translate the 429-with-`budget_exhausted` body into
  the C6 terminal path (the PoC's `BudgetExhausted`).
- **Telemetry**: the SDK depends on `lmnr` (Laminar) but initializes it only
  when `LMNR_*` env vars are set — off by default; keep those vars out of
  the crew-pod env. `OPENHANDS_SUPPRESS_BANNER=1` silences the import
  banner.

## The peer: Claude Agent SDK — why not (for this role)

The Claude Agent SDK (Python) is "Claude Code as a library": it bundles the
Claude Code CLI (Node) and drives it as a subprocess. It matches OpenHands
on the tool criteria (`tools: []` strips all built-ins; custom tools are
in-process MCP servers) and has the most mature permission pipeline of any
harness. It fails this platform on structural grounds:

- **Model lock-in**: it speaks the Anthropic Messages format to an
  Anthropic-shaped endpoint, and Anthropic explicitly does not support
  routing to non-Claude models through a gateway. That breaks the
  llm-gateway's tier routing and metering as built (OpenAI
  chat-completions), and with it the mock-brain e2e economy — the suite's
  62 free green runs are a hard requirement.
- **A subprocess in the loop**: the reasoning loop is a spawned Node CLI,
  not a Python library call — a worse fit for the crew pod and for the
  activity-dispatches-into-the-brain contract.
- **Maturity/terms**: pre-1.0 (0.2.x with breaking patch releases); use
  governed by Anthropic's Commercial Terms, vs. plain MIT.

Its permission-pipeline advantage doesn't transfer: Poseidon's enforcement
point is OPA at the gateway, not in-process permissions.

Rest of the field, briefly: **mini-swe-agent** is a deliberately minimal
research harness (no MCP, no permissioning) — a baseline, not a substrate;
**smolagents** executes model-written Python as its action model, inverting
the every-side-effect-through-a-vetted-tool control point; **LangGraph**
(incl. Open SWE/Deep Agents) is a build-your-own-harness kit — i.e. what
the PoC already is; the point of step 22 is adopting a maintained harness,
not rebuilding one. OpenHands and the Claude Agent SDK are the only two
mainstream leaderboard-grade coding harnesses packaged as embeddable
libraries, which is why they were the head-to-head.

## O-3: why the six existing tools win

- **No framework pressure for richer primitives.** The fidelity argument
  for patch-apply/search assumed the brain might come with tools expecting
  them. It doesn't: the Hands adapter authors the model-facing schemas
  itself (exactly as the PoC toolbox does), and OpenHands' built-in
  bash/editor tools are opt-in imports we simply don't install.
- **`sandbox.exec` already subsumes the candidates** — search is `grep`/
  `git grep`, patch-apply is `git apply` or `write_file`, all inside the
  confined workspace. The PoC brain and the mock brain both operate on the
  six today; the compiled Rego (`RUN_SCOPED`) binds exactly this set.
- **The escalation path is cheap and known** if step-22 parity shows the
  adopted brain underperforming on whole-file writes: a new primitive is a
  five-file change — toolserver route (`packages/sandbox/.../toolserver/app.py`),
  `SANDBOX_ROUTES` (`gateway/service.py`), an `@mcp.tool` wrapper
  (`gateway/app.py`), `SANDBOX_TOOLS` in `schema/tools.py` (which lands it
  in `RUN_SCOPED`/`KNOWN_TOOLS` and the compiled `bound_to_run` Rego for
  free), and the catalog templates — plus regoldening. The planner's
  explicit `_PLANNER_TOOLS` list would need a deliberate call before any
  read-only `search` joins it.
- **Concurrency stays honest.** The toolserver's handlers are sync and
  serialize on the threadpool; the SDK's `tool_concurrency_limit` defaults
  to 1. Aligned today — if parallel tool calls are ever wanted, both ends
  change together.

## Adapter sketch (Hands — consumed at step 22)

`packages/agent` is rebuilt as a brain host. What stays, what maps:

**Kept verbatim — the C6 contract.** `app.py`'s
`POST /task`/`GET /task/{run_id}`, the four modes
(`implement|plan|deliver|review`), 409-on-running (the orchestrator's
unbounded-retry safety), and the `TaskStatus` fields. `gateway_client.py`
(MCP streamable-HTTP, bearer auth) and `config.py` (token file → env
resolution) carry over unchanged.

**Per-mode agent construction.** One `Agent` per dispatch mode, differing
only in the tool list — mirroring `toolbox.py`'s phase sets:

```python
def build_agent(mode: str, settings, run_id: str, token: str) -> Agent:
    llm = LLM(
        model=settings.model,
        base_url=settings.llm_proxy_url,          # the llm-gateway
        api_key=token,                            # same workload credential
        extra_headers={"X-Poseidon-Run-Id": run_id},
        num_retries=1,                            # never grind on budget 429s
        usage_id=run_id,
    )
    tools = {
        "implement": ACT_TOOLS,                   # list_dir/read_file/write_file/exec
        "deliver":   ACT_TOOLS + [OPEN_PR],       # composite appears only here
        "plan":      PLANNER_TOOLS,
        "review":    REVIEWER_TOOLS,              # reads + run_tests + PR comment, no writes
    }[mode]
    return Agent(
        llm=llm,
        tools=tools,
        condenser=LLMSummarizingCondenser(llm=llm, max_size=..., keep_first=...),
        system_prompt=render_role_prompt(mode, settings),
    )
```

The implement agent **structurally lacks `open_pr`** — delivery before the
platform verify gate stays impossible by construction, same guarantee as
the PoC's phase toolsets.

**Tool layer.** One `ToolDefinition` per model-facing PoC schema; each
executor closes over `(gateway_client, run_id, target_repo)`:

- `run_id` and `target_repo` are injected at call time and never appear in
  any action schema — the model cannot see or forge them (`toolbox.py`'s
  first mechanism, carried over).
- `open_pr` stays a composite: executor calls `sandbox.git_bundle` then
  `git.open_pr`, so `bundle_b64` never enters LLM context (the second
  mechanism, carried over).
- A gateway denial (`ToolCallError`, carrying the OPA rule name) is caught
  in the executor and returned as an error `Observation` — denial-as-tool-
  result, preserving the E2 recovery path the injection tests assert.
- A 429 `budget_exhausted` from the llm-gateway maps to the C6
  failed-with-budget outcome so the workflow's budget pause keeps working.

**Conversation per dispatch.** `Conversation(agent, workspace=<inert scratch
dir>, visualizer=None, max_iteration_per_run=settings.max_iterations)`,
in-memory persistence (the pod is ephemeral; Temporal owns durability and
retries — the activity dispatches in and waits, nothing else). The
completion claim is the finish-tool message; on `mode="implement"` the
dispatch returns claim-only (`pr_url=None`), never a self-delivery.

**Off by omission (enforced boundaries, §4.3):**

- `openhands-tools` (bash/editor/browser built-ins) is not a dependency —
  the brain's only effectors are the Hands tools.
- `TaskToolSet` is never registered — no SDK sub-agents, so no identity
  acquisition outside the crew mechanism.
- `DockerWorkspace`/`RemoteWorkspace` unused — the orchestrator owns all
  sandbox lifecycle.
- The SDK's confirmation policies and security analyzer stay off — Law is
  the gateway + OPA; brain-internal verification is advisory (the platform
  verify gate is the gate of record).
- `LMNR_*` env vars absent from the crew pod; `OPENHANDS_SUPPRESS_BANNER=1`
  set.

**Pinning.** `openhands-sdk==<exact>` in `packages/agent/pyproject.toml`;
upgrades are deliberate commits gated on the parity suite.

**Regression bar for step 22** (unchanged from the plan): the 8
`test_graph.py` behaviors re-expressed against the new host, then the kind
e2e trio (`test_happy_path.py`, `test_injection.py`, `test_verify_gate.py`)
green on the mock brain, then delete the LangGraph graph.

## Appendix — the headless proof

`openhands-sdk` 1.40.0, no network, no provider. Abridged to the
load-bearing parts:

```python
from openhands.sdk import Agent, Conversation, LLM, register_tool
from openhands.sdk.tool import ToolDefinition, ToolExecutor
from openhands.sdk.tool.spec import Tool
from openhands.sdk.tool.schema import Action, Observation

class GatewayAction(Action):
    command: str = Field(description="command to send to the Poseidon gateway")

class GatewayExecutor(ToolExecutor):
    def __call__(self, action, conversation=None):
        calls.append(action.command)              # stub for gateway_client.call(...)
        return GatewayObservation(output=f"gateway-ok: {action.command}")

class SandboxExecTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state=None, **params):
        return [cls(name="sandbox_exec", description="...",
                    action_type=GatewayAction, observation_type=GatewayObservation,
                    executor=GatewayExecutor())]

register_tool("SandboxExecTool", SandboxExecTool)

llm = LLM(model="openai/mock-model", base_url="http://llm-gateway.local/v1",
          api_key="run-scoped-jwt", usage_id="spike")
agent = Agent(llm=llm, tools=[Tool(name="SandboxExecTool")])

# scripted() returns: turn 1 → sandbox_exec("pytest -q"); turn 2 → finish
with patch("openhands.sdk.llm.llm.litellm_completion", side_effect=scripted):
    convo = Conversation(agent=agent, workspace="./spike-ws", visualizer=None)
    convo.send_message("Run the test suite via the gateway.")
    convo.run()
```

Output:

```
tools available to agent: ['FinishTool', 'ThinkTool'] + custom: ['SandboxExecTool']
agent status: ConversationExecutionStatus.FINISHED
gateway calls made: ['pytest -q']
SPIKE OK: headless loop, custom-tool-only, mock LLM
```

Key sources: github.com/OpenHands/software-agent-sdk · docs.openhands.dev/sdk
(custom-tools, mcp, workspace, context-condenser, security,
convo-pause-and-resume, task-tool-set guides) ·
github.com/anthropics/claude-agent-sdk-python ·
code.claude.com/docs/en/agent-sdk (overview, custom-tools, permissions,
llm-gateway) · github.com/SWE-agent/mini-swe-agent ·
github.com/huggingface/smolagents · github.com/langchain-ai/open-swe
