"""Brain-host tests: the production OpenHands SDK loops
driven by a scripted LLM and a fake gateway (see conftest)."""

from conftest import (
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

from agent.brain import (
    build_llm,
    decide_dispatch,
    deliver_dispatch,
    implement_dispatch,
    plan_dispatch,
    pm_dispatch,
    review_dispatch,
)
from agent.hands import REVIEWER_TOOLS

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
        result = implement_dispatch(settings, RUN_ID, TASK)

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


def test_deliver_dispatch_commits_bundles_and_opens_pr(settings, gateway):
    """The workflow-directed delivery (mode=deliver): commit, bundle, PR —
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
                ),
            ),
            says("Delivered."),
        ]
    )

    with scripted(gateway, llm):
        result = deliver_dispatch(settings, RUN_ID, TASK)

    assert result.pr_url == PR_URL
    assert result.error is None
    assert gateway.tools_called() == [
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
    """The reviewer (mode=review): read the diff, execute the suite, post
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
            calls(tc("verdict", verdict="pass", summary="Suite green; in scope.")),
            says("Verdict recorded."),
        ]
    )

    with scripted(gateway, llm):
        result = review_dispatch(
            settings, RUN_ID, "Review pull request #41 (url): Add greet"
        )

    assert result.error is None
    assert result.comment_urls == [COMMENT_URL]
    assert result.verdict == "pass"
    assert result.verdict_summary == "Suite green; in scope."
    assert gateway.tools_called() == [
        "git.read",
        "sandbox.run_tests",
        "git.comment",
    ]
    # Every offered toolset is the reviewer's (plus the SDK's control
    # tools) — read-and-judge only.
    for index in range(len(llm.requests)):
        offered = set(llm.offered(index)) - {"finish", "think"}
        assert offered == set(REVIEWER_TOOLS)
    assert "write_file" not in REVIEWER_TOOLS and "open_pr" not in REVIEWER_TOOLS


def test_review_without_findings_fails_the_dispatch(settings, gateway):
    llm = ScriptedLLM([says("Looks fine, nothing to say.")])

    with scripted(gateway, llm):
        result = review_dispatch(settings, RUN_ID, "Review pull request #41")

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
        result = review_dispatch(settings, RUN_ID, "Review pull request #41")

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
        implement_dispatch(settings, RUN_ID, TASK)

    messages, tools = llm.requests[0]
    assert messages[0]["role"] == "system"
    system = text_of(messages[0])
    assert "feature/" in system
    assert settings.target_repo in system
    assert "open_pr" not in system  # the prompt matches the tool surface
    task_message = text_of(messages[1])
    assert TASK in task_message
    assert "README.md" in task_message  # workspace survey rode the prompt
    assert "carry it out" not in task_message  # no delivery talk while acting

    offered = set(llm.offered(0)) - {"finish", "think"}
    assert offered == {"list_dir", "read_file", "write_file", "exec"}
    assert "open_pr" not in llm.offered(0)  # no delivery tool, ever, here


def test_deliver_prompt_names_the_delivery_tool(settings, gateway):
    """The deliver dispatch's prompt is the delivery procedure: it names
    open_pr and the feature-branch rule."""
    llm = ScriptedLLM([says("stopping without delivering")])

    with scripted(gateway, llm):
        deliver_dispatch(settings, RUN_ID, TASK)

    system = text_of(llm.requests[0][0][0])
    assert "open_pr" in system
    assert "feature/" in system
    assert settings.target_repo in system


def test_review_prompt_is_the_reviewers(settings, gateway):
    """The reviewer is told it is a reviewer — comment_pr procedure, not an
    engineer identity."""
    llm = ScriptedLLM([says("nothing to say")])

    with scripted(gateway, llm):
        review_dispatch(settings, RUN_ID, "Review pull request #41")

    system = text_of(llm.requests[0][0][0])
    assert "comment_pr" in system
    assert "backend-engineer-1" not in system
    assert "write_file" not in system


def test_plan_prompt_asks_for_the_brief_only(settings, gateway):
    """The planner's loop holds the read tools and nothing else, and its
    prompt asks for the brief without ever telling the model to carry the
    work out."""
    llm = ScriptedLLM([says("1. Write slugify.py")])

    with scripted(gateway, llm):
        plan_dispatch(settings, RUN_ID, TASK)

    messages, _ = llm.requests[0]
    assert set(llm.offered(0)) - {"finish", "think"} == {"list_dir", "read_file"}
    system = text_of(messages[0])
    user = text_of(messages[1])
    assert "brief" in system
    assert "open_pr" not in system and "write_file" not in system
    assert "carry it out" not in system and "carry it out" not in user
    assert "numbered brief" in user


def test_out_of_scope_tool_call_feeds_back_and_loop_recovers(settings, gateway):
    """A model call to a tool outside the phase surface (open_pr while
    implementing) comes back as an error tool result — never a crash, and
    never a gateway call."""
    llm = ScriptedLLM(
        [
            calls(tc("open_pr", branch="feature/x", title="t", body="b")),
            says("Understood — no delivery from here."),
        ]
    )

    with scripted(gateway, llm):
        result = implement_dispatch(settings, RUN_ID, TASK)

    assert result.error is None
    assert "git.open_pr" not in gateway.tools_called()
    retry_messages, _ = llm.requests[-1]
    feedback = text_of(retry_messages[-1])
    assert "open_pr" in feedback and "not found" in feedback


# --- verify loop and iteration cap ---------------------------------------------


def test_failing_tests_loop_back_with_report_until_iteration_cap(settings, gateway):
    gateway.default_test_result = TESTS_FAIL
    # One act turn per verify cycle; tests never pass, so the run fails
    # after max_iterations (3) verify cycles.
    llm = ScriptedLLM([says("try 1"), says("try 2"), says("try 3")])

    with scripted(gateway, llm):
        result = implement_dispatch(settings, RUN_ID, TASK)

    assert result.pr_url is None
    assert result.iterations == settings.max_iterations
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
            calls(tc("open_pr", branch="main", title="t", body="b")),
            # After seeing the denial, comply with policy.
            calls(tc("open_pr", branch="feature/greet", title="t", body="b")),
            says("Delivered on the feature branch."),
        ]
    )

    with scripted(gateway, llm):
        result = deliver_dispatch(settings, RUN_ID, TASK)

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
        result = deliver_dispatch(settings, RUN_ID, TASK)

    assert result.pr_url is None
    assert result.error == "agent stopped without opening a pull request"


# --- budget stop ----------------------------------------------------------------


def test_budget_429_surfaces_the_marker_without_retry_grind(settings, gateway):
    """The LLM gateway's hard stop is a 429 with the budget_exhausted
    marker; the dispatch surfaces it (the workflow keys its pause on the
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

    import agent.brain as brain

    with patch.object(brain, "GatewayBridge", lambda url, token: FakeBridge(gateway)):
        with patch("openhands.sdk.llm.llm.litellm_completion", side_effect=raise_429):
            try:
                implement_dispatch(settings, RUN_ID, TASK)
                raised = None
            except Exception as exc:
                raised = exc

    assert raised is not None
    assert "budget_exhausted" in str(raised)
    assert len(attempts) == 1  # num_retries=1: one attempt, no grind


# --- planner mode ---------------------------------------------------------------


def test_planner_reads_then_answers_with_the_brief(settings, gateway):
    """mode=plan: a survey, a few reads, then the brief — the agent's
    final text, which the executor's dispatch carries verbatim."""
    llm = ScriptedLLM(
        [
            calls(tc("read_file", path="README.md")),
            says("1. Write slugify.py\n2. Deliver on `feature/x`."),
        ]
    )

    with scripted(gateway, llm):
        result = plan_dispatch(settings, RUN_ID, TASK)

    assert result.error is None
    assert result.brief == "1. Write slugify.py\n2. Deliver on `feature/x`."
    assert gateway.tools_called() == ["sandbox.list_dir", "sandbox.read_file"]
    assert TASK in text_of(llm.requests[0][0][1])


def test_planner_brief_may_arrive_through_finish(settings, gateway):
    llm = ScriptedLLM([calls(tc("finish", message="1. Write slugify.py"))])

    with scripted(gateway, llm):
        result = plan_dispatch(settings, RUN_ID, TASK)

    assert result.error is None
    assert result.brief == "1. Write slugify.py"


def test_planner_empty_brief_is_an_error(settings, gateway):
    """Finishing with nothing to say is not a brief. (An empty plain
    reply never reaches here: the SDK re-prompts on it until the step
    cap, which is the error above.)"""
    llm = ScriptedLLM([calls(tc("finish", message=""))])

    with scripted(gateway, llm):
        result = plan_dispatch(settings, RUN_ID, TASK)

    assert result.error == "planner produced an empty brief"


def test_planner_that_never_answers_is_an_error(settings, gateway):
    """A planner that only reads until the step cap has no brief to give."""
    from agent.brain import PLAN_STEP_CAP

    llm = ScriptedLLM([calls(tc("read_file", path="README.md"))] * PLAN_STEP_CAP)

    with scripted(gateway, llm):
        result = plan_dispatch(settings, RUN_ID, TASK)

    assert result.brief is None
    assert result.error == f"planner did not finish within {PLAN_STEP_CAP} steps"


# --- the LLM client shape -------------------------------------------------------


def test_llm_carries_run_attribution_and_single_attempt(settings):
    llm = build_llm(settings, RUN_ID, "tok")
    assert llm.extra_headers == {"X-Poseidon-Run-Id": RUN_ID}
    assert llm.num_retries == 1
    assert llm.model == "openai/test-model"  # provider prefix for routing only
    assert llm.base_url == settings.llm_proxy_url


# --- prompt rendering -----------------------------------------------------------


def test_prompt_rendering_survives_stray_braces(tmp_path, settings):
    """Prompt files are edited as prose: a brace added to the text must
    stay literal text, never a format crash or a format-spec evaluation."""
    from agent.brain import _render_prompt

    template = tmp_path / "prompt.md"
    template.write_text(
        'Repo: {target_repo}. Example: {"key": "value"} and {not_a_field!r}.'
    )
    rendered = _render_prompt(template, settings)
    assert f"Repo: {settings.target_repo}." in rendered
    assert '{"key": "value"}' in rendered
    assert "{not_a_field!r}" in rendered


# --- decide mode --------------------------------------------------------------


OBSERVATION = (
    "Conduct tick. Runs in flight: 0. Spent today: $0.00.\n"
    "Startable actions: implement, review, idle\n\n"
    "Open tickets:\n#5 — Add slugify\n\n"
    "Delivered pull requests awaiting review:\n(none)\n\n"
    "Recent runs:\n(none)\n\n"
    "Reply with exactly two lines:\n"
    "DECISION: implement #<n> | review #<n> | idle\n"
    "RATIONALE: one line saying why."
)


def test_decide_parses_the_two_line_contract(settings, gateway):
    llm = ScriptedLLM([says("DECISION: implement #5\nRATIONALE: oldest open work.")])
    with scripted(gateway, llm):
        result = decide_dispatch(settings, RUN_ID, OBSERVATION)
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
    assert "conduct decision" in text_of(messages[0])


def test_decide_idle_and_review_shapes(settings, gateway):
    for reply, expected in [
        ("DECISION: idle\nRATIONALE: nothing worth starting.", ("idle", None)),
        ("DECISION: review #7\nRATIONALE: finish first.", ("review", 7)),
    ]:
        with scripted(gateway, ScriptedLLM([says(reply)])):
            result = decide_dispatch(settings, RUN_ID, OBSERVATION)
        assert (result.decision["action"], result.decision["target"]) == expected


def test_decide_without_a_rationale_fails(settings, gateway):
    """The journal is half the point of asking: an unexplained decision is
    a failed dispatch, not a silently accepted one."""
    with scripted(gateway, ScriptedLLM([says("DECISION: implement #5")])):
        result = decide_dispatch(settings, RUN_ID, OBSERVATION)
    assert result.decision is None
    assert "broke the two-line contract" in result.error


def test_decide_rejects_prose_and_unknown_actions(settings, gateway):
    for reply in [
        "I think we should implement #5 next.",
        "DECISION: deploy #5\nRATIONALE: ship it.",
    ]:
        with scripted(gateway, ScriptedLLM([says(reply)])):
            result = decide_dispatch(settings, RUN_ID, OBSERVATION)
        assert result.decision is None and result.error


# --- prompt composition ---------------------------------------------------------


def test_system_prompt_is_the_procedure_then_sdk_heuristics(settings, gateway):
    """The mode's own procedure opens the system prompt; the OpenHands
    sections that fit the mode's use of the workspace follow it: a
    dispatch that writes code gets the file and code-quality guidance, a
    reader gets efficiency only, the PM gets no workspace advice at all."""
    llm = ScriptedLLM([says("nothing to do")])
    with scripted(gateway, llm):
        implement_dispatch(settings, RUN_ID, TASK)
    system = text_of(llm.requests[0][0][0])
    assert system.startswith("# Poseidon executor")
    assert "<EFFICIENCY>" in system
    assert "<FILE_SYSTEM_GUIDELINES>" in system
    assert "<CODE_QUALITY>" in system
    assert "file_fix.py" in system  # the no-suffixed-copies rule, verbatim
    assert "<PULL_REQUESTS>" not in system  # the SDK's PR workflow is not ours
    assert "OpenHands agent" not in system  # nor its identity

    llm = ScriptedLLM([says("nothing to say")])
    with scripted(gateway, llm):
        review_dispatch(settings, RUN_ID, "Review pull request #41")
    system = text_of(llm.requests[0][0][0])
    assert "<EFFICIENCY>" in system
    assert "<FILE_SYSTEM_GUIDELINES>" not in system

    llm = ScriptedLLM([says("done")])
    with scripted(gateway, llm):
        pm_dispatch(settings, RUN_ID, "goal")
    system = text_of(llm.requests[0][0][0])
    assert system.startswith("You are the product manager")
    assert "<EFFICIENCY>" not in system


def test_model_family_guidance_follows_the_model():
    """The SDK's per-family block renders for a model it recognises and
    stays out for one it does not (the mock brain)."""
    from agent.prompting import CODING_SECTIONS, compose

    claude = compose("# procedure", CODING_SECTIONS, "anthropic/claude-sonnet-4-5")
    assert claude.startswith("# procedure\n\n<EFFICIENCY>")
    assert "<IMPORTANT>" in claude
    mock = compose("# procedure", CODING_SECTIONS, "openai/mock-brain")
    assert "<IMPORTANT>" not in mock
    assert "<CODE_QUALITY>" in mock
