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


def next_completion(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> MockCompletion:
    task_text = _task_text(messages)
    offered = _offered(tools)

    if not offered:  # plan: the graph passes no tools
        return _text(PLAN_TEXT)

    if "open_pr" not in offered:  # act
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
