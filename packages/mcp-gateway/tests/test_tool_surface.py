"""The whole tool surface through the full gateway pipeline: each
tool callable by its authorized role and denied (audited) for every other,
`git.merge_pr` restricted to the platform lifecycle, and `crew.request`
recording idempotent spawn intent under the conductor identity."""

import json

import pytest
from conftest import (
    AGENT_TOKEN,
    PM_TOKEN,
    REVIEWER_TOKEN,
    RUNNER_TOKEN,
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
    """A bound ephemeral reviewer (the review workflow's sdet crew member):
    its credential is its pod's projected SA token, and the participants
    row — looked up by SA name — is the whole identity."""
    participants.add(
        "reviewer-01X",
        RUN_ID,
        "pm-1",
        "feature-review",
        "reviewer",
        company="hello-saas",
        role_template="sdet",
        namespace="hello-saas",
        service_account="crew-reviewer-01x",
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

        updated = payload(
            await session.call_tool(
                "issues.update",
                {"run_id": RUN_ID, "repo": REPO, "number": 7, "state": "closed"},
            )
        )
        assert updated["state"] == "closed"

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
        ("issues.update", "allow"),
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


async def test_engineer_is_denied_the_pm_surface(app, audit, backends):
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


# -- git.merge_pr (platform only) -------------------------------------------


async def test_merge_is_platform_only(app, backends, audit):
    async with mcp_session(app, RUNNER_TOKEN) as session:
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


# -- crew.request (authorization + intent, never fulfillment) ----------------


async def test_crew_request_records_idempotent_intent(app, crew_requests, audit):
    async with mcp_session(app, PM_TOKEN) as session:
        first = payload(
            await session.call_tool(
                "crew.request",
                {
                    "run_id": RUN_ID,
                    "crew_template": "feature-delivery",
                    "reason": "ticket #7",
                },
            )
        )
        again = payload(
            await session.call_tool(
                "crew.request",
                {
                    "run_id": RUN_ID,
                    "crew_template": "feature-delivery",
                    "reason": "ticket #7, retried",
                },
            )
        )

    assert first["status"] == "recorded"
    assert first["conductor"] == "pm-1"
    assert first["expires_at"]  # expiring: no orphan intent
    assert again["id"] == first["id"]  # idempotent
    assert len(crew_requests.rows) == 1

    event = audit_events(audit)[-1]
    assert event["tool"] == "crew.request" and event["decision"] == "allow"


async def test_crew_request_outside_may_spawn_is_denied(app, crew_requests, audit):
    """The compiled rule narrows the template to the conductor's may_spawn
    list — an unknown or unauthorized crew is a rule deny, recorded nowhere."""
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "crew.request",
            {"run_id": RUN_ID, "crew_template": "shadow-ops", "reason": "hm"},
        )
    assert result.isError and "default_deny" in result.content[0].text
    assert crew_requests.rows == {}
    assert audit_events(audit)[-1]["decision"] == "deny"


async def test_engineer_cannot_request_crews(app, crew_requests):
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "crew.request",
            {"run_id": RUN_ID, "crew_template": "feature-delivery", "reason": "r"},
        )
    assert result.isError
    assert crew_requests.rows == {}


async def test_over_cap_crew_request_is_denied_and_audited(
    app, participants, crew_requests, audit
):
    """The conductor's live spawn count rides the decision input and
    the compiled cap denies by rule — a first-class governance event."""
    participants.add("fd-planner-a", "run-a", "pm-1", "feature-delivery", "planner")
    participants.add("fr-reviewer-b", "run-b", "pm-1", "feature-review", "reviewer")

    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "crew.request",
            {
                "run_id": RUN_ID,
                "crew_template": "feature-delivery",
                "reason": "a third crew",
            },
        )
    assert result.isError
    assert "crew_request_over_cap" in result.content[0].text
    assert crew_requests.rows == {}  # nothing recorded on a rule deny
    event = audit_events(audit)[-1]
    assert event["decision"] == "deny" and event["rule"] == "crew_request_over_cap"


async def test_exhausted_budget_denies_crew_request(app, ledger, crew_requests, audit):
    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="backend-engineer-1",
        parent_agent_id="",
        role="backend-engineer",
        model="mock-brain",
        prompt_tokens=1,
        completion_tokens=1,
        usd=5.0,
    )
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "crew.request",
            {"run_id": RUN_ID, "crew_template": "feature-delivery", "reason": "r"},
        )
    assert result.isError
    assert "crew_request_no_budget" in result.content[0].text
    assert crew_requests.rows == {}
    assert audit_events(audit)[-1]["rule"] == "crew_request_no_budget"
