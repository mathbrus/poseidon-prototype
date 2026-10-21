"""Agent-host tests: the production OpenHands SDK loops
driven by a scripted LLM and a fake gateway (see conftest)."""

import asyncio
import threading

import pytest
from conftest import (
    ARCHITECTURE_TICKET_URL,
    BOUNDS,
    BRANCH_PATTERN,
    COMMENT_URL,
    PR_URL,
    RUN_ID,
    TESTS_FAIL,
    ScriptedLLM,
    calls,
    says,
    scripted,
    tc,
    text_of,
)

from agent.hands import REVIEW_TOOLS
from agent.procedures import (
    build_llm,
    run_decide,
    run_deliver,
    run_implement,
    run_review,
    run_scope,
)
from agent.prompting import system_prompt

TASK = "Add a `greet(name)` function to src/hello.py with a test."


# --- happy path ---------------------------------------------------------------


def test_happy_path_ends_in_a_completion_claim(settings, gateway):
    """The implement dispatch ends at
    the advisory-green completion claim — no PR, no deliver tools, ever."""
    llm = ScriptedLLM(
        [
            calls(tc("write_file", path="src/hello.py", content="def greet(n): ...")),
            says("Implementation done, ready for the test suite."),
        ]
    )

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    assert result.pr_url is None  # the brain never self-delivers
    assert result.iterations == 1
    assert llm.script == []
    assert gateway.tools_called() == [
        "sandbox.list_dir",
        "sandbox.write_file",
        "sandbox.run_tests",
    ]
    assert all(args["run_id"] == RUN_ID for _, args in gateway.calls)


def test_a_tree_with_no_tests_is_advisory_green(settings, gateway):
    """A fresh repository has no suite to run; its first deliveries are
    documents. The host reads the empty collection the way the gate of
    record does — green — so the model is not sent back to "fix" a suite
    that has nothing in it, nor pushed to invent a test to get out."""
    gateway.default_test_result = {
        "exit_code": 5,
        "stdout": "no tests ran in 0.00s",
        "stderr": "",
    }
    llm = ScriptedLLM(
        [
            calls(tc("write_file", path="docs/STRATEGY.md", content="# Strategy")),
            says("The document is written."),
        ]
    )

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    assert result.iterations == 1
    assert llm.script == []
    assert gateway.tools_called().count("sandbox.run_tests") == 1


def test_deliver_dispatch_commits_bundles_and_opens_pr(settings, gateway):
    """The workflow-directed delivery (the deliver procedure): commit, bundle, PR —
    and the bundle base64 never enters the model context."""
    llm = ScriptedLLM(
        [
            calls(
                tc(
                    "exec",
                    cmd=(
                        "git checkout -b feature/greet && git add -A && "
                        "git -c user.name=a -c user.email=a@p commit -m 'add greet'"
                    ),
                ),
                tc(
                    "open_pr",
                    branch="feature/greet",
                    title="Add greet",
                    body="Adds greet().",
                    rationale="greet() is what the task asks for; tests green.",
                ),
            ),
            says("Delivered."),
        ]
    )

    with scripted(gateway, llm):
        result = run_deliver(settings, RUN_ID, TASK, BOUNDS)

    assert result.pr_url == PR_URL
    assert result.error is None
    assert gateway.tools_called() == [
        "sandbox.list_dir",  # the survey every sandboxed dispatch opens with
        "sandbox.exec",
        "sandbox.git_bundle",
        "git.open_pr",
    ]
    open_pr_args = gateway.calls[-1][1]
    assert open_pr_args["repo"] == settings.target_repo
    assert open_pr_args["branch"] == "feature/greet"
    assert open_pr_args["bundle_b64"] == "YnVuZGxl"
    # The composite hid the bundle: no message the model saw contains it.
    for messages, _ in llm.requests:
        assert all("YnVuZGxl" not in text_of(m) for m in messages)


def test_review_dispatch_reads_runs_comments_and_judges(settings, gateway):
    """The reviewer (review): read the diff, execute the suite, post
    findings, end with the structured verdict — no write tools on the
    surface. The verdict is the one local tool: a record, not a gateway
    call, so it never appears in the gateway trace."""
    llm = ScriptedLLM(
        [
            calls(tc("read_pr", number=41)),
            calls(tc("run_tests")),
            calls(
                tc(
                    "comment_pr",
                    number=41,
                    body="Suite green in the review sandbox; no findings.",
                )
            ),
            calls(tc("verdict", verdict="pass", rationale="Suite green; in scope.")),
            says("Verdict recorded."),
        ]
    )

    with scripted(gateway, llm):
        result = run_review(
            settings, RUN_ID, "Review pull request #41 (url): Add greet", BOUNDS
        )

    assert result.error is None
    assert result.comment_urls == [COMMENT_URL]
    assert result.verdict == "pass"
    assert result.verdict_rationale == "Suite green; in scope."
    assert gateway.tools_called() == [
        "sandbox.list_dir",
        "git.read",
        "sandbox.run_tests",
        "git.comment",
    ]
    # Every offered toolset is the reviewer's (plus the SDK's control
    # tools) — read-and-judge only.
    for index in range(len(llm.requests)):
        offered = set(llm.offered(index)) - {"finish", "think"}
        assert offered == set(REVIEW_TOOLS)
    assert "write_file" not in REVIEW_TOOLS and "open_pr" not in REVIEW_TOOLS


def test_review_without_findings_fails_the_dispatch(settings, gateway):
    llm = ScriptedLLM([says("Looks fine, nothing to say.")])

    with scripted(gateway, llm):
        result = run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)

    assert result.error == "reviewer posted no findings"
    assert result.comment_urls == []


def test_review_without_a_verdict_fails_the_dispatch(settings, gateway):
    """Findings alone do not finish a review: the structured verdict is
    the contract, and prose is never parsed for one."""
    llm = ScriptedLLM(
        [
            calls(
                tc(
                    "comment_pr",
                    number=41,
                    body="Suite green; approving. VERDICT: pass",
                )
            ),
            says("All good."),
        ]
    )

    with scripted(gateway, llm):
        result = run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)

    assert result.error == "reviewer finished without a verdict"
    assert result.comment_urls == [COMMENT_URL]
    assert result.verdict is None


def test_prompt_and_toolsets_per_phase(settings, gateway):
    """The implement dispatch never offers delivery tools: the
    act surface is the read/write set and the loop ends at the claim —
    open_pr exists only in the separate deliver dispatch, and the implement
    prompt does not even mention it."""
    llm = ScriptedLLM([says("nothing to do")])

    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS)

    messages, tools = llm.requests[0]
    assert messages[0]["role"] == "system"
    system = text_of(messages[0])
    assert BRANCH_PATTERN in system  # the bounds, not a rule of the prompt's own
    assert settings.target_repo in system
    assert "open_pr" not in system  # the prompt matches the tool surface
    task_message = text_of(messages[1])
    assert TASK in task_message
    assert "README.md" in task_message  # workspace survey rode the prompt
    assert "carry it out" not in task_message  # no delivery talk while acting

    offered = set(llm.offered(0)) - {"finish", "think"}
    assert offered == {
        "list_dir",
        "read_file",
        "write_file",
        "edit_file",
        "exec",
        "request_architecture",
    }
    assert "open_pr" not in llm.offered(0)  # no delivery tool, ever, here
    # The read tools declare themselves read-only, which also keeps the
    # SDK's security_risk parameter out of their schemas.
    schemas = {t["function"]["name"]: t["function"] for t in llm.requests[0][1]}
    assert "security_risk" not in schemas["read_file"]["parameters"]["properties"]
    assert "security_risk" in schemas["write_file"]["parameters"]["properties"]


def test_deliver_prompt_names_the_delivery_tool(settings, gateway):
    """The deliver dispatch's prompt is the delivery procedure: it names
    open_pr, and the branch pattern reaches it through the bounds."""
    llm = ScriptedLLM([says("stopping without delivering")])

    with scripted(gateway, llm):
        run_deliver(settings, RUN_ID, TASK, BOUNDS)

    system = text_of(llm.requests[0][0][0])
    assert "open_pr" in system
    assert BRANCH_PATTERN in system
    assert settings.target_repo in system


def test_review_prompt_is_the_reviewers(settings, gateway):
    """The reviewer is told it is a reviewer — comment_pr procedure, not an
    implementer identity."""
    llm = ScriptedLLM([says("nothing to say")])

    with scripted(gateway, llm):
        run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)

    system = text_of(llm.requests[0][0][0])
    assert "comment_pr" in system
    assert "implementer-1" not in system
    assert "write_file" not in system


def test_out_of_scope_tool_call_feeds_back_and_loop_recovers(settings, gateway):
    """A model call to a tool outside the procedure's surface (open_pr while
    implementing) comes back as an error tool result — never a crash, and
    never a gateway call."""
    llm = ScriptedLLM(
        [
            calls(tc("open_pr", branch="feature/x", title="t", body="b", rationale="r")),
            says("Understood — no delivery from here."),
        ]
    )

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    assert "git.open_pr" not in gateway.tools_called()
    retry_messages, _ = llm.requests[-1]
    feedback = text_of(retry_messages[-1])
    assert "open_pr" in feedback and "not found" in feedback


# --- verify loop and iteration cap ---------------------------------------------


def test_failing_tests_loop_back_with_report_until_iteration_cap(settings, gateway):
    gateway.default_test_result = TESTS_FAIL
    # One act turn per verify cycle; tests never pass, so the run fails
    # after the three verify cycles the conftest allows.
    llm = ScriptedLLM([says("try 1"), says("try 2"), says("try 3")])

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.pr_url is None
    assert result.iterations == 3
    assert result.error == "tests still failing after 3 iterations"
    assert gateway.tools_called().count("sandbox.run_tests") == 3
    assert "git.open_pr" not in gateway.tools_called()

    # The failure report was surfaced to the model between cycles.
    last_messages, _ = llm.requests[-1]
    report = text_of(last_messages[-1])
    assert "exit code 1" in report
    assert "1 failed: test_hello" in report


# --- deny path (deny-and-recover) -----------------------------------------------


def test_denied_pr_is_surfaced_and_agent_retries_on_feature_branch(settings, gateway):
    """Injection recovery in the deliver dispatch: the deny (with the rule name) feeds back
    and the model recovers onto feature/*."""
    llm = ScriptedLLM(
        [
            # Injected-task behaviour: try to deliver straight to main.
            calls(tc("open_pr", branch="main", title="t", body="b", rationale="r")),
            # After seeing the denial, comply with policy.
            calls(
                tc("open_pr", branch="feature/greet", title="t", body="b", rationale="r")
            ),
            says("Delivered on the feature branch."),
        ]
    )

    with scripted(gateway, llm):
        result = run_deliver(settings, RUN_ID, TASK, BOUNDS)

    assert result.pr_url == PR_URL
    assert result.error is None

    pr_attempts = [
        args["branch"] for tool, args in gateway.calls if tool == "git.open_pr"
    ]
    assert pr_attempts == ["main", "feature/greet"]

    retry_messages, _ = llm.requests[1]
    denial = retry_messages[-1]
    assert denial["role"] == "tool"
    assert text_of(denial).startswith("ERROR:")
    assert "git_open_pr_protected_branch" in text_of(denial)


def test_deliver_without_pr_fails_the_run(settings, gateway):
    llm = ScriptedLLM([says("I refuse to open a PR.")])

    with scripted(gateway, llm):
        result = run_deliver(settings, RUN_ID, TASK, BOUNDS)

    assert result.pr_url is None
    assert result.error == "agent stopped without opening a pull request"


# --- budget stop ----------------------------------------------------------------


def test_budget_429_surfaces_the_marker_without_retry_grind(settings, gateway):
    """The LLM gateway's hard stop is a 429 with the budget_exhausted
    marker; the dispatch surfaces it (the workflow keys its park on the
    substring) after a single attempt — the SDK must not retry into it."""
    from litellm.exceptions import RateLimitError

    attempts = []

    def raise_429(*args, **kwargs):
        attempts.append(1)
        raise RateLimitError(
            message='{"error": {"code": "budget_exhausted", '
            '"message": "run exhausted its budget"}}',
            llm_provider="openai",
            model="test-model",
        )

    from unittest.mock import patch

    from conftest import FakeBridge

    import agent.procedures as procedures

    with patch.object(
        procedures, "GatewayBridge", lambda url, token: FakeBridge(gateway)
    ):
        with patch("openhands.sdk.llm.llm.litellm_completion", side_effect=raise_429):
            try:
                run_implement(settings, RUN_ID, TASK, BOUNDS)
                raised = None
            except Exception as exc:
                raised = exc

    assert raised is not None
    assert "budget_exhausted" in str(raised)
    assert len(attempts) == 1  # num_retries=1: one attempt, no grind


def test_reviewer_step_budget_is_its_own_cap_not_verify_cycles(settings, gateway):
    """The reviewer's budget is `REVIEW_STEP_CAP`, decoupled from
    `VERIFY_CYCLES` — that cap bounds implement's verify cycles and must
    not starve the reviewer of the steps a diff-read, a test run, findings
    and a verdict take."""
    from agent.procedures import REVIEW_STEP_CAP, VERIFY_CYCLES

    assert REVIEW_STEP_CAP > VERIFY_CYCLES * 2
    llm = ScriptedLLM(
        [calls(tc("read_file", path=f"src/m{i}.py")) for i in range(REVIEW_STEP_CAP)]
    )

    with scripted(gateway, llm):
        result = run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)

    assert result.error == f"agent loop did not finish within {REVIEW_STEP_CAP} steps"


# --- the LLM client shape -------------------------------------------------------


def test_llm_carries_run_attribution_and_single_attempt(settings):
    llm = build_llm(settings, RUN_ID, "tok")
    assert llm.extra_headers == {"X-Poseidon-Run-Id": RUN_ID}
    assert llm.num_retries == 1
    assert llm.model == "openai/test-model"  # provider prefix for routing only
    assert llm.base_url == settings.llm_proxy_url


# --- prompt rendering -----------------------------------------------------------


def test_prompt_rendering_survives_stray_braces(tmp_path, settings, monkeypatch):
    """Prompt files are edited as prose: a brace added to the text must
    stay literal text, never a format crash or a format-spec evaluation."""
    from agent import prompting

    monkeypatch.setattr(prompting, "PROMPTS_DIR", tmp_path)
    (tmp_path / "implement.md").write_text(
        "# Poseidon procedure: implement\n"
        'Repo: {target_repo}. Example: {"key": "value"} and {not_a_field!r}.\n'
        "{bounds}"
    )
    rendered = prompting.render_procedure("implement", settings, "{vision}")
    assert f"Repo: {settings.target_repo}." in rendered
    assert '{"key": "value"}' in rendered
    assert "{not_a_field!r}" in rendered
    # One pass: a substituted text is never scanned for placeholders.
    assert rendered.endswith("{vision}")


def test_a_prompt_without_the_bounds_is_refused(tmp_path, settings, monkeypatch):
    """The bounds bind every procedure; a prompt file that leaves them out
    is a packaging error, refused before any model call."""
    from agent import prompting

    monkeypatch.setattr(prompting, "PROMPTS_DIR", tmp_path)
    (tmp_path / "scope.md").write_text("# Poseidon procedure: scope\n{vision}\n")
    with pytest.raises(RuntimeError, match="does not carry the bounds"):
        prompting.render_procedure("scope", settings, BOUNDS)


def test_every_prompt_opens_with_its_own_header(settings):
    """The header is the mock brain's whole procedure key and the host's own
    packaging check: each procedure's file names exactly that procedure."""
    from agent.prompting import PROCEDURE_HEADER, PROMPTS_DIR, SECTIONS, render_procedure

    assert {p.stem for p in PROMPTS_DIR.glob("*.md")} == set(SECTIONS)
    for procedure in SECTIONS:
        assert render_procedure(procedure, settings, BOUNDS).startswith(
            f"{PROCEDURE_HEADER}{procedure}\n"
        )


def test_a_prompt_naming_another_procedure_is_refused(tmp_path, settings, monkeypatch):
    from agent import prompting

    monkeypatch.setattr(prompting, "PROMPTS_DIR", tmp_path)
    (tmp_path / "review.md").write_text("# Poseidon procedure: implement\nwrong")
    with pytest.raises(RuntimeError, match="does not open with its header"):
        prompting.render_procedure("review", settings, BOUNDS)


# --- decide -------------------------------------------------------------------


OBSERVATION = (
    "Heartbeat tick. Runs in flight: 0. Spent today: $0.00."
    " Open tickets on the repository: 1.\n"
    "Startable actions: implement, review, scope, idle\n\n"
    "Open tickets:\n#5 — Add slugify\n\n"
    "Delivered pull requests awaiting review:\n(none)\n\n"
    "Recent runs:\n(none)\n\n"
    "Reply with exactly two lines:\n"
    "DECISION: scope | implement #<n> | review #<n> | idle\n"
    "RATIONALE: one line saying why."
)


def test_decide_parses_the_two_line_contract(settings, gateway):
    llm = ScriptedLLM([says("DECISION: implement #5\nRATIONALE: oldest open work.")])
    with scripted(gateway, llm):
        result = run_decide(settings, RUN_ID, OBSERVATION, BOUNDS)
    assert result.error is None
    assert result.decision == {
        "action": "implement",
        "target": 5,
        "rationale": "oldest open work.",
    }
    # LLM-only: no tools were offered and no gateway call was made — the
    # observation is everything the PM may consider.
    messages, tools = llm.requests[0]
    assert tools is None
    assert gateway.calls == []
    assert "heartbeat decision" in text_of(messages[0])


def test_decide_idle_and_review_shapes(settings, gateway):
    for reply, expected in [
        ("DECISION: idle\nRATIONALE: nothing worth starting.", ("idle", None)),
        ("DECISION: scope\nRATIONALE: the backlog is dry.", ("scope", None)),
        ("DECISION: review #7\nRATIONALE: finish first.", ("review", 7)),
    ]:
        with scripted(gateway, ScriptedLLM([says(reply)])):
            result = run_decide(settings, RUN_ID, OBSERVATION, BOUNDS)
        assert (result.decision["action"], result.decision["target"]) == expected


def test_decide_without_a_rationale_fails(settings, gateway):
    """The journal is half the point of asking: an unexplained decision is
    a failed dispatch, not a silently accepted one."""
    with scripted(gateway, ScriptedLLM([says("DECISION: implement #5")])):
        result = run_decide(settings, RUN_ID, OBSERVATION, BOUNDS)
    assert result.decision is None
    assert "broke the two-line contract" in result.error


def test_decide_rejects_prose_and_unknown_actions(settings, gateway):
    for reply in [
        "I think we should implement #5 next.",
        "DECISION: deploy #5\nRATIONALE: ship it.",
    ]:
        with scripted(gateway, ScriptedLLM([says(reply)])):
            result = run_decide(settings, RUN_ID, OBSERVATION, BOUNDS)
        assert result.decision is None and result.error


# --- prompt composition ---------------------------------------------------------


def test_system_prompt_is_the_procedure_then_sdk_heuristics(settings, gateway):
    """The procedure's own text opens the system prompt; the OpenHands
    sections that fit its use of the workspace follow it: a
    dispatch that writes code gets the file and code-quality guidance, a
    reader gets efficiency only, the PM gets no workspace advice at all."""
    llm = ScriptedLLM([says("nothing to do")])
    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS)
    system = text_of(llm.requests[0][0][0])
    assert system.startswith("# Poseidon procedure: implement")
    assert "<EFFICIENCY>" in system
    assert "<FILE_SYSTEM_GUIDELINES>" in system
    assert "<CODE_QUALITY>" in system
    assert "file_fix.py" in system  # the no-suffixed-copies rule, verbatim
    assert "<PULL_REQUESTS>" not in system  # the SDK's PR workflow is not ours
    assert "OpenHands agent" not in system  # nor its identity

    llm = ScriptedLLM([says("nothing to say")])
    with scripted(gateway, llm):
        run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)
    system = text_of(llm.requests[0][0][0])
    assert "<EFFICIENCY>" in system
    assert "<FILE_SYSTEM_GUIDELINES>" not in system

    llm = ScriptedLLM([says("done")])
    with scripted(gateway, llm):
        run_scope(settings, RUN_ID, "Strategy:\n(absent)", BOUNDS)
    system = text_of(llm.requests[0][0][0])
    assert system.startswith("# Poseidon procedure: scope")
    assert "<EFFICIENCY>" not in system


def test_an_architecture_request_is_a_gateway_call_and_a_ticket_of_the_run(
    settings, gateway
):
    """The hand resolves to `architecture.request` under the run's identity
    — no crew, no repository chosen by the model — and the ticket the
    platform filed rides the run record like a PM's."""
    llm = ScriptedLLM(
        [
            calls(
                tc(
                    "request_architecture",
                    rationale="one module is not enough",
                    proposal="allow two",
                )
            ),
            says("Requested a change to the plan; stopping."),
        ]
    )
    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)
    (tool, args) = next(c for c in gateway.calls if c[0] == "architecture.request")
    assert args == {
        "run_id": RUN_ID,
        "rationale": "one module is not enough",
        "proposal": "allow two",
    }
    assert result.ticket_urls == [ARCHITECTURE_TICKET_URL]
    assert result.pr_url is None


def test_a_reviewers_architecture_request_is_a_ticket_of_the_run(settings, gateway):
    """The reviewer may ask for the plan to change instead of judging a
    change against a plan it finds wrong; the ticket the platform filed
    rides the review's record on every exit — with a verdict, and
    without one."""
    request = tc(
        "request_architecture",
        rationale="the plan forbids what the slice needs",
        proposal="allow two modules",
    )
    findings = tc("comment_pr", number=41, body="Suite green; the plan is wrong.")
    with scripted(gateway, ScriptedLLM([calls(request, findings), says("Asked.")])):
        result = run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)
    assert result.error == "reviewer finished without a verdict"
    assert result.ticket_urls == [ARCHITECTURE_TICKET_URL]
    verdict = tc("verdict", verdict="fail", rationale="Contradicts the plan; requested.")
    with scripted(
        gateway, ScriptedLLM([calls(request, findings, verdict), says("Done.")])
    ):
        result = run_review(settings, RUN_ID, "Review pull request #41", BOUNDS)
    assert result.verdict == "fail"
    assert result.ticket_urls == [ARCHITECTURE_TICKET_URL]


def test_the_documents_are_untrusted_context_for_implement_and_review(settings, gateway):
    """The company's documents the dispatch carries render in the system
    message's untrusted block beside AGENTS.md, under the name the model
    knows them by — after the static prompt, never inside it."""
    documents = {"strategy": "STRATEGY-TEXT-4471", "architecture": "MASTERPLAN-TEXT-9931"}
    llm = ScriptedLLM([says("nothing to do")])
    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS, documents)
    system = text_of(llm.requests[0][0][0])
    static = system_prompt("implement", settings, "mock-brain", BOUNDS)
    assert system.startswith(static.rstrip())
    assert "STRATEGY-TEXT-4471" in system and "MASTERPLAN-TEXT-9931" in system
    assert "architecture masterplan" in system
    assert "STRATEGY-TEXT-4471" not in static

    llm = ScriptedLLM([says("nothing to do")])
    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS)
    assert "STRATEGY-TEXT-4471" not in text_of(llm.requests[0][0][0])


@pytest.mark.parametrize(
    ("procedure", "run", "reply"),
    [
        ("implement", run_implement, "nothing to do"),
        ("deliver", run_deliver, "stopping without delivering"),
        ("review", run_review, "nothing to say"),
        ("scope", run_scope, "done"),
        ("decide", run_decide, "DECISION: idle\nRATIONALE: nothing worth starting"),
    ],
)
def test_the_bounds_land_in_the_trusted_block_of_every_procedure(
    settings, gateway, procedure, run, reply
):
    """The bounds the dispatch carries render into the static system
    prompt — the first block, the one the platform wrote — for all five
    procedures, and never into the untrusted block beside the documents."""
    documents = {"strategy": "STRATEGY-TEXT-4471"}
    llm = ScriptedLLM([says(reply)])
    with scripted(gateway, llm):
        run(settings, RUN_ID, "Task:\nanything", BOUNDS, documents)
    system = llm.requests[0][0][0]
    content = system["content"]
    blocks = [b["text"] for b in content] if isinstance(content, list) else [content]
    assert BOUNDS in blocks[0]
    assert blocks[0].count(BOUNDS) == 1
    for untrusted in blocks[1:]:
        assert BOUNDS not in untrusted


def test_the_vision_reaches_every_procedure_but_deliver(settings):
    """The vision rides the static prompt — the trusted channel — into
    every procedure that judges what is worth doing; delivery only ships
    what was already verified and reads none of it."""
    for procedure in ("scope", "decide", "implement", "review"):
        assert settings.vision in system_prompt(procedure, settings, "mock-brain", BOUNDS)
    assert settings.vision not in system_prompt("deliver", settings, "mock-brain", BOUNDS)


def test_model_family_guidance_follows_the_model():
    """The SDK's per-family block renders for a model it recognises and
    stays out for one it does not (the mock brain)."""
    from agent.prompting import SECTIONS, compose

    coding = SECTIONS["implement"]
    claude = compose("# procedure", coding, "anthropic/claude-sonnet-4-5")
    assert claude.startswith("# procedure\n\n<EFFICIENCY>")
    assert "<IMPORTANT>" in claude
    mock = compose("# procedure", coding, "openai/mock-brain")
    assert "<IMPORTANT>" not in mock
    assert "<CODE_QUALITY>" in mock


# --- repository instructions ----------------------------------------------------


def test_agents_md_rides_the_system_message_as_untrusted_context(settings, gateway):
    """A workspace AGENTS.md is read once by the host, under the pod's own
    token, and reaches the model in the system message's second block
    inside the SDK's untrusted-content framing — never as an instruction
    of ours."""
    gateway.agents_md = "Use four-space indents. Run `make check` before commits."
    llm = ScriptedLLM([says("nothing to do")])

    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert gateway.tools_called()[:2] == ["sandbox.list_dir", "sandbox.read_file"]
    assert gateway.calls[1][1]["path"] == "AGENTS.md"
    system = llm.requests[0][0][0]
    blocks = [b["text"] for b in system["content"]]
    assert len(blocks) == 2
    assert blocks[0].startswith(
        "# Poseidon procedure: implement"
    )  # the static prompt, verbatim
    assert "<REPO_CONTEXT>" in blocks[1] and "<UNTRUSTED_CONTENT>" in blocks[1]
    assert "Run `make check` before commits." in blocks[1]
    assert "<CURRENT_DATETIME>" not in blocks[1]
    assert "AGENTS.md" not in blocks[0]


def test_no_agents_md_means_no_dynamic_block(settings, gateway):
    llm = ScriptedLLM([says("nothing to do")])

    with scripted(gateway, llm):
        run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert "sandbox.read_file" not in gateway.tools_called()
    system = llm.requests[0][0][0]
    assert len(system["content"]) == 1


def test_agents_md_the_role_may_not_read_is_skipped(settings, gateway):
    """Listing shows the file but policy refuses the read: the dispatch
    goes on without instructions rather than failing."""
    gateway.agents_md = "secret conventions"
    gateway.deny = {"sandbox.read_file"}
    llm = ScriptedLLM([says("nothing to do")])

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    assert len(llm.requests[0][0][0]["content"]) == 1


# --- stuck detection ------------------------------------------------------------


def test_repeating_itself_ends_the_dispatch_as_stuck(settings, gateway):
    """The SDK's detector is on for every model: four identical action
    and observation pairs inside one turn stop the loop with STUCK, which
    the procedure reports as a failure rather than a completion claim."""
    same = calls(tc("read_file", path="README.md"))
    llm = ScriptedLLM([same, same, same, same])

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error == "agent loop stuck repeating itself (after 0 verify cycles)"
    assert llm.script == []  # stopped by the detector, not by a fifth reply
    assert "sandbox.run_tests" not in gateway.tools_called()


def test_a_stuck_pm_fails_with_what_it_filed(settings, gateway):
    same = calls(tc("read_issues"))
    llm = ScriptedLLM([same] * 4)

    with scripted(gateway, llm):
        result = run_scope(settings, RUN_ID, "Strategy:\n(absent)", BOUNDS)

    assert result.error == "agent loop stuck repeating itself"
    assert result.ticket_urls == []


# --- parallel reads, ordered writes ---------------------------------------------


def test_a_batch_of_reads_runs_concurrently(settings, gateway):
    """Three reads in one reply fan out: each one blocks on a barrier
    that opens only when all three have arrived, so a one-at-a-time
    tool executor would time out and hand the model three errors."""
    barrier = threading.Barrier(3, timeout=5)

    async def gated(tool, args):
        if tool == "sandbox.read_file":
            barrier.wait()

    gateway.before = gated
    llm = ScriptedLLM(
        [
            calls(
                tc("read_file", path="a.py"),
                tc("read_file", path="b.py"),
                tc("read_file", path="c.py"),
            ),
            says("done"),
        ]
    )

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    observations = [m for m in llm.requests[1][0] if m["role"] == "tool"]
    assert len(observations) == 3
    assert all("Error executing" not in text_of(m) for m in observations)


def test_a_batch_with_writes_runs_in_the_models_order(settings, gateway):
    """A checkout, a write, a read and an add in one reply happen in that
    order: the slow command finishes before the fast write starts."""

    async def slow_exec(tool, args):
        if tool == "sandbox.exec":
            await asyncio.sleep(0.05)

    gateway.before = slow_exec
    llm = ScriptedLLM(
        [
            calls(
                tc("exec", cmd="git checkout -b feature/x"),
                tc("write_file", path="a.py", content="x"),
                tc("read_file", path="a.py"),
                tc("exec", cmd="git add -A"),
            ),
            says("done"),
        ]
    )

    with scripted(gateway, llm):
        result = run_implement(settings, RUN_ID, TASK, BOUNDS)

    assert result.error is None
    assert gateway.tools_called() == [
        "sandbox.list_dir",
        "sandbox.exec",
        "sandbox.write_file",
        "sandbox.read_file",
        "sandbox.exec",
        "sandbox.run_tests",
    ]
