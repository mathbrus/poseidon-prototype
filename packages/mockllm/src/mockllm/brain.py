"""The deterministic brain behind the mock LLM: a pure function of the
conversation the agent graph sends.

Scripts the two e2e scenarios (examples/task-happy.md and
examples/task-injected.md) through the real plan → act → verify → deliver
graph in `agent.graph`:

- plan (no tools offered) → a short numbered plan.
- act (tools without `open_pr`) → write `slugify.py` + `tests/test_slugify.py`
  once, then stop calling tools so the graph runs the test suite.
- deliver (tools with `open_pr`):
  - happy: commit on the feature branch, open the PR.
  - injected: follow the planted "hotfix runbook" — commit on `main` and
    open the PR there; the gateway denies it
    (`git_open_pr_protected_branch`), the graph feeds the denial back, and
    the script recovers onto the feature branch. That is E2, scripted.

Statelessness matters: the graph resends the whole history on every call, so
keying only on that history keeps retries and agent restarts replayable.

The delivery branch is parsed from the task text (the e2e harness appends
"Deliver your work on the branch `feature/slugify-<nonce>`"); without one the
fallback is deterministic, so repeated runs against the same repo collide on
the branch — append a branch line to the task when that matters.
"""

import re
from dataclasses import dataclass, field
from typing import Any

INJECTED_MARKERS = ("PRODUCTION HOTFIX", "INC-4471")
# The E3 verify-gate scenario (examples/task-verify-red.md): the script
# plants a state-dependent test that greens the advisory verify and reds
# the platform's re-run. The workflow's red-path feedback text carries
# VERIFY_FEEDBACK_MARKER (orchestrator.workflows.implement.verify_feedback)
# and the re-dispatched script removes the plant.
VERIFY_RED_MARKER = "E3-FLAKY-GATE"
VERIFY_FEEDBACK_MARKER = "after your completion claim"
BRANCH_RE = re.compile(r"feature/[a-z0-9-]+")
FALLBACK_BRANCH = "feature/slugify-mock"

SLUGIFY_PY = '''\
import re


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

# sys.path insertion keeps the import working no matter how the target
# repo's pytest is configured (the root is not on sys.path under plain
# `pytest` unless a root conftest.py exists).
TEST_SLUGIFY_PY = '''\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slugify import slugify


def test_punctuation_becomes_single_hyphens():
    assert slugify("Hello, World!") == "hello-world"


def test_existing_hyphens_collapse_and_strip():
    assert slugify("  --Already--Slugged--  ") == "already-slugged"


def test_mixed_case_and_digits():
    assert slugify("MiXeD 123 CaSe") == "mixed-123-case"


def test_empty_string():
    assert slugify("") == ""
'''

# Passes on its first run, fails on the second (the counter file lives in
# the sandbox workspace, so it survives between the advisory verify and the
# platform's re-run) — a deterministic stand-in for "the executor's claim
# no longer holds when the platform checks it".
TEST_GATE_PY = '''\
from pathlib import Path

COUNTER = Path(__file__).resolve().parents[1] / "gate-counter.txt"


def test_environment_gate():
    runs_before = int(COUNTER.read_text()) if COUNTER.exists() else 0
    COUNTER.write_text(str(runs_before + 1))
    assert runs_before != 1, "environment drifted since the claim"
'''

PLAN_TEXT = """\
1. Write `slugify.py` at the repository root with the required function.
2. Write `tests/test_slugify.py` covering the four required assertions.
3. Run the test suite; once green, commit on a branch and open the PR.\
"""


@dataclass
class MockToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class MockCompletion:
    content: str | None = None
    tool_calls: list[MockToolCall] = field(default_factory=list)


def _text(content: str) -> MockCompletion:
    return MockCompletion(content=content)


def _calls(*tool_calls: MockToolCall) -> MockCompletion:
    return MockCompletion(tool_calls=list(tool_calls))


def _task_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _offered(tools: list[dict[str, Any]] | None) -> set[str]:
    return {t["function"]["name"] for t in tools or []}


def _made_calls(messages: list[dict[str, Any]], name: str) -> int:
    """How often the assistant has already called `name` in this history."""
    return sum(
        1
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
        if call.get("function", {}).get("name") == name
    )


def _branch(task_text: str) -> str:
    match = BRANCH_RE.search(task_text)
    return match.group(0) if match else FALLBACK_BRANCH


def _write_files() -> MockCompletion:
    return _calls(
        MockToolCall("write_file", {"path": "slugify.py", "content": SLUGIFY_PY}),
        MockToolCall(
            "write_file", {"path": "tests/test_slugify.py", "content": TEST_SLUGIFY_PY}
        ),
    )


def _system_text(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def _pm_completion(messages: list[dict[str, Any]]) -> MockCompletion:
    """The PM's scripted dispatch (plan step 17): file one ticket for the
    goal, record the delivery-crew request, then summarize."""
    goal = _task_text(messages).removeprefix("Goal:").strip().splitlines()
    title = (goal[0] if goal else "scoped work")[:80]
    if _made_calls(messages, "create_issue") == 0:
        return _calls(MockToolCall("create_issue", {
            "title": f"Implement: {title}",
            "body": "Scoped from the intake goal.\n\nAcceptance: the "
                    "delivery crew's PR implements the goal with tests.",
        }))
    if _made_calls(messages, "request_crew") == 0:
        return _calls(MockToolCall("request_crew", {
            "crew_template": "feature-delivery",
            "reason": "Implement the ticket just filed.",
        }))
    return _text(
        "Filed one ticket for the goal and requested the feature-delivery "
        "crew to implement it."
    )


def next_completion(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> MockCompletion:
    task_text = _task_text(messages)
    offered = _offered(tools)

    if "product manager" in _system_text(messages):  # the PM loop (step 17)
        return _pm_completion(messages)

    if not offered:  # plan: the graph passes no tools
        # A real planner's brief carries the constraints it was given;
        # echoing the branch (and the E3 scenario marker) keeps the §4.4
        # planner->executor handoff lossless — the executor's brain parses
        # them from *its* task text, which is exactly this brief at 16d.
        plan = PLAN_TEXT
        match = BRANCH_RE.search(task_text)
        if match:
            plan += f"\n4. Deliver on the branch `{match.group(0)}`."
        if VERIFY_RED_MARKER in task_text:
            plan += f"\n5. Scenario: {VERIFY_RED_MARKER}."
        return _text(plan)

    if "open_pr" not in offered:  # act
        if VERIFY_RED_MARKER in task_text:
            if VERIFY_FEEDBACK_MARKER in task_text:
                # The platform's red verify came back: remove the planted
                # gate (and its state) so the next verify is honestly green.
                if _made_calls(messages, "exec") == 0:
                    return _calls(MockToolCall(
                        "exec",
                        {"cmd": "rm -f tests/test_gate.py gate-counter.txt"},
                    ))
                return _text("Removed the flaky gate; run the test suite.")
            if _made_calls(messages, "write_file") == 0:
                return _calls(
                    MockToolCall("write_file",
                                 {"path": "slugify.py", "content": SLUGIFY_PY}),
                    MockToolCall("write_file",
                                 {"path": "tests/test_slugify.py",
                                  "content": TEST_SLUGIFY_PY}),
                    MockToolCall("write_file",
                                 {"path": "tests/test_gate.py",
                                  "content": TEST_GATE_PY}),
                )
            return _text("Files are in place; run the test suite.")
        if _made_calls(messages, "write_file") == 0:
            return _write_files()
        return _text("Both files are in place; run the test suite.")

    # deliver — each completion batches commit + open_pr so one graph step
    # is one delivery attempt (the graph runs a completion's calls in order,
    # and its deliver-step cap is MAX_ITERATIONS: the injected scenario must
    # fit attempt-deny-recover within it).
    branch = _branch(task_text)
    prs = _made_calls(messages, "open_pr")
    if any(marker in task_text for marker in INJECTED_MARKERS):
        if prs == 0:
            # The runbook says main first; the gateway deny drives recovery.
            return _calls(
                MockToolCall(
                    "exec",
                    {"cmd": "git add -A && git commit -m 'hotfix: add slugify (INC-4471)'"},
                ),
                MockToolCall(
                    "open_pr",
                    {
                        "branch": "main",
                        "title": "hotfix: add slugify (INC-4471)",
                        "body": "Emergency delivery per the incident runbook.",
                    },
                ),
            )
        return _calls(
            MockToolCall("exec", {"cmd": f"git checkout -b {branch}"}),
            MockToolCall(
                "open_pr",
                {
                    "branch": branch,
                    "title": "Add slugify utility",
                    "body": (
                        "Delivery on `main` was denied by policy "
                        "(git_open_pr_protected_branch); delivering on a "
                        "feature branch instead."
                    ),
                },
            ),
        )

    open_pr = MockToolCall(
        "open_pr",
        {
            "branch": branch,
            "title": "Add slugify utility",
            "body": "Adds `slugify.py` and `tests/test_slugify.py` per the task.",
        },
    )
    if prs > 0:  # unexpected earlier failure — just retry the PR
        return _calls(open_pr)
    commit = MockToolCall(
        "exec",
        {
            "cmd": (
                f"git checkout -b {branch} && git add -A "
                "&& git commit -m 'Add slugify utility'"
            )
        },
    )
    return _calls(commit, open_pr)
