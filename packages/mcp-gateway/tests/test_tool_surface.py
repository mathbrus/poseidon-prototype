"""The whole tool surface through the full gateway pipeline: each
tool callable by its authorized role and denied (audited) for every other,
and `git.merge_pr` restricted to the platform lifecycle."""

import json

import pytest
from conftest import (
    AGENT_TOKEN,
    PM_TOKEN,
    REVIEWER_TOKEN,
    WORKER_TOKEN,
    audit_events,
    mcp_session,
)

pytestmark = pytest.mark.anyio

RUN_ID = "run-01TOOLS"
REPO = "github.test/example/target"


def payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def reviewer_token(participants) -> str:
    """A bound ephemeral reviewer (the review workflow's reviewer ephemeral agent):
    its credential is its pod's projected SA token, and the participants
    row — looked up by SA name — is the whole identity."""
    participants.add(
        "reviewer-01X",
        RUN_ID,
        "pm-1",
        "feature-review",
        "reviewer",
        company="hello-saas",
        namespace="hello-saas",
        service_account="ephemeral-reviewer-01x",
    )
    return REVIEWER_TOKEN


# -- issues.* (the PM's surface) --------------------------------------------


async def test_pm_runs_the_issue_lifecycle(app, backends, audit):
    async with mcp_session(app, PM_TOKEN) as session:
        created = payload(
            await session.call_tool(
                "issues.create",
                {"run_id": RUN_ID, "repo": REPO, "title": "ship slugs", "body": "spec"},
            )
        )
        assert created["number"] == 7 and created["state"] == "open"

        backlog = payload(
            await session.call_tool("issues.read", {"run_id": RUN_ID, "repo": REPO})
        )
        # The GitHub issues listing includes PRs; the backlog must not.
        assert [i["number"] for i in backlog["issues"]] == [7]

        comment = payload(
            await session.call_tool(
                "issues.comment",
                {"run_id": RUN_ID, "repo": REPO, "number": 7, "body": "done"},
            )
        )
        assert comment["url"]

    events = [(e["tool"], e["decision"]) for e in audit_events(audit)]
    assert events == [
        ("issues.create", "allow"),
        ("issues.read", "allow"),
        ("issues.comment", "allow"),
    ]
    assert all(e["agent_id"] == "pm-1" for e in audit_events(audit))


async def test_single_issue_read_returns_the_view(app, backends):
    async with mcp_session(app, PM_TOKEN) as session:
        result = payload(
            await session.call_tool(
                "issues.read", {"run_id": RUN_ID, "repo": REPO, "number": 7}
            )
        )
    assert result["issues"] == [
        {
            "number": 7,
            "title": "an issue",
            "state": "open",
            "body": "details",
            "url": "https://github.test/i/7",
        }
    ]


async def test_single_issue_read_refuses_a_pull_request(app, backends):
    # GitHub serves PRs from the issues endpoint; PRs belong to git.read.
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "issues.read", {"run_id": RUN_ID, "repo": REPO, "number": 8}
        )
    assert result.isError and "pull request" in result.content[0].text


async def test_implementer_is_denied_the_pm_surface(app, audit, backends):
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "issues.create",
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b"},
        )
    assert result.isError and "default_deny" in result.content[0].text
    event = audit_events(audit)[-1]
    assert event["tool"] == "issues.create" and event["decision"] == "deny"
    assert not backends.github_calls  # nothing reached GitHub


async def test_pm_is_denied_the_review_surface(app, audit):
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "git.read", {"run_id": RUN_ID, "repo": REPO, "number": 5}
        )
    assert result.isError and "default_deny" in result.content[0].text


# -- git.read / git.comment (the reviewer's surface) -------------------------


async def test_reviewer_reads_and_comments_the_pr(app, participants, audit):
    token = reviewer_token(participants)
    async with mcp_session(app, token) as session:
        pr = payload(
            await session.call_tool(
                "git.read", {"run_id": RUN_ID, "repo": REPO, "number": 5}
            )
        )
        assert pr["head"] == "feature/x"
        assert pr["files"][0]["path"] == "app.py"

        comment = payload(
            await session.call_tool(
                "git.comment",
                {"run_id": RUN_ID, "repo": REPO, "number": 5, "body": "LGTM-ish"},
            )
        )
        assert comment["url"]

    for event in audit_events(audit):
        assert event["decision"] == "allow"
        assert event["lifecycle"] == "ephemeral"
        assert event["parent_agent_id"] == "pm-1"


async def test_reviewer_cannot_merge_or_open_issues(app, participants):
    token = reviewer_token(participants)
    async with mcp_session(app, token) as session:
        merge = await session.call_tool(
            "git.merge_pr", {"run_id": RUN_ID, "repo": REPO, "number": 5}
        )
        create = await session.call_tool(
            "issues.create",
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b"},
        )
    assert merge.isError and create.isError


# -- repo.read_file (platform only) ------------------------------------------


async def test_reading_a_document_is_platform_only(app, backends, audit):
    """The company's documents are read at base_ref by the platform —
    present or honestly absent — and by nobody in the company."""
    async with mcp_session(app, WORKER_TOKEN) as session:
        strategy = payload(
            await session.call_tool(
                "repo.read_file",
                {
                    "run_id": RUN_ID,
                    "repo": REPO,
                    "ref": "main",
                    "path": "docs/STRATEGY.md",
                },
            )
        )
        masterplan = payload(
            await session.call_tool(
                "repo.read_file",
                {
                    "run_id": RUN_ID,
                    "repo": REPO,
                    "ref": "main",
                    "path": "docs/ARCHITECTURE.md",
                },
            )
        )
    assert strategy == {"present": True, "text": "# Strategy\n\nShip slugs.\n"}
    assert masterplan == {"present": False, "text": None}
    assert backends.github_calls[-1][0].endswith("/contents/docs/ARCHITECTURE.md")

    for token in (PM_TOKEN, AGENT_TOKEN):
        async with mcp_session(app, token) as session:
            result = await session.call_tool(
                "repo.read_file",
                {
                    "run_id": RUN_ID,
                    "repo": REPO,
                    "ref": "main",
                    "path": "docs/STRATEGY.md",
                },
            )
        assert result.isError, "a company role read a document through the platform"


# -- git.merge_pr (platform only) -------------------------------------------


async def test_merge_is_platform_only(app, backends, audit):
    async with mcp_session(app, WORKER_TOKEN) as session:
        merged = payload(
            await session.call_tool(
                "git.merge_pr", {"run_id": RUN_ID, "repo": REPO, "number": 5}
            )
        )
    assert merged == {"merged": True, "sha": "abc123"}
    assert backends.github_calls[-1][0].endswith("/pulls/5/merge")

    for token in (PM_TOKEN, AGENT_TOKEN):
        async with mcp_session(app, token) as session:
            result = await session.call_tool(
                "git.merge_pr", {"run_id": RUN_ID, "repo": REPO, "number": 5}
            )
        assert result.isError, "a company role reached git.merge_pr"
    denies = [
        e
        for e in audit_events(audit)
        if e["tool"] == "git.merge_pr" and e["decision"] == "deny"
    ]
    assert len(denies) == 2
