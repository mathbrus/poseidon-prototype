"""The whole tool surface through the full gateway pipeline: each
tool callable by its authorized role and denied (audited) for every other,
and `git.merge_pr` restricted to the platform lifecycle."""

import json

import pytest
from conftest import (
    AGENT_TOKEN,
    HEAD_SHA,
    PM_TOKEN,
    REVIEWER_TOKEN,
    WORKER_TOKEN,
    audit_events,
    mcp_session,
    register_sandbox,
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


async def test_pm_runs_the_issue_lifecycle(app, backends, audit, filed):
    async with mcp_session(app, PM_TOKEN) as session:
        created = payload(
            await session.call_tool(
                "issues.create",
                {
                    "run_id": RUN_ID,
                    "repo": REPO,
                    "title": "ship slugs",
                    "body": "spec",
                    "rationale": "the strategy's next slice",
                },
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
    # The filing carries the PM's own reason in cleartext; the reads do not.
    assert [e["rationale"] for e in audit_events(audit)] == [
        "the strategy's next slice",
        None,
        None,
    ]
    assert all(e["agent_id"] == "pm-1" for e in audit_events(audit))
    # The ticket is on the registry the moment the host created it, under
    # the run that filed it: the loop reads its own writes from there.
    assert filed.filed_since(REPO, 60) == [7]


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


async def test_a_ticket_without_a_rationale_is_refused_before_policy(
    app, audit, backends
):
    """A committing call owes the caller's reason: without one the call is
    malformed — refused with what is owed, nothing decided, nothing
    recorded, nothing reaching GitHub."""
    async with mcp_session(app, PM_TOKEN) as session:
        for args in (
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b"},
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b", "rationale": " "},
        ):
            result = await session.call_tool("issues.create", args)
            assert result.isError and "rationale" in result.content[0].text
    assert audit_events(audit) == [] and not backends.github_calls


async def test_implementer_is_denied_the_pm_surface(app, audit, backends):
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "issues.create",
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b", "rationale": "r"},
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
        assert pr["head_sha"] == HEAD_SHA
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
            "git.merge_pr",
            {"run_id": RUN_ID, "repo": REPO, "number": 5, "sha": HEAD_SHA},
        )
        create = await session.call_tool(
            "issues.create",
            {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b", "rationale": "r"},
        )
    assert merge.isError and create.isError


# -- architecture.request (asking is not writing) ----------------------------


async def test_an_architecture_request_becomes_a_stewardship_ticket(app, backends, audit):
    """The bound implementer asks once: the platform files the proposal as
    a ticket on the company's repository with the stewardship trailer —
    the caller named neither — and the audit event is the receipt. The
    second ask in the same run is denied by the compiled cap, read off
    that receipt."""
    await register_sandbox(app, RUN_ID)  # the run the caller is bound to
    async with mcp_session(app, AGENT_TOKEN) as session:
        first = payload(
            await session.call_tool(
                "architecture.request",
                {
                    "run_id": RUN_ID,
                    "rationale": "the plan allows one module",
                    "proposal": "Allow a second module.\nFor the statistics.",
                },
            )
        )
        second = await session.call_tool(
            "architecture.request",
            {"run_id": RUN_ID, "rationale": "again", "proposal": "again"},
        )
    assert first["url"] == "https://github.test/i/7"
    path, body, _ = backends.github_calls[-1]
    assert path.endswith("/issues")
    assert (
        body["title"] == f"Architecture request from run {RUN_ID}: Allow a second module."
    )
    assert body["body"].endswith("Crew: stewardship")
    assert "the plan allows one module" in body["body"]
    assert second.isError and "architecture_request_over_cap" in second.content[0].text
    events = [e for e in audit_events(audit) if e["tool"] == "architecture.request"]
    assert [e["decision"] for e in events] == ["allow", "deny"]
    assert backends.policy_inputs[-1]["architecture_requests"] == 1

    async with mcp_session(app, PM_TOKEN) as session:
        pm = await session.call_tool(
            "architecture.request", {"run_id": RUN_ID, "rationale": "r", "proposal": "p"}
        )
    assert pm.isError, "the PM reached architecture.request"


# -- repo.present (platform only) --------------------------------------------


async def test_which_declared_paths_the_base_ref_holds_is_platform_only(
    app, backends, audit
):
    """The contract tree a review replays is read at base_ref by the
    platform: present where the host holds it, honestly absent where the
    company has not written it yet — and by nobody in the company."""
    args = {
        "run_id": RUN_ID,
        "repo": REPO,
        "ref": "main",
        "paths": ["tests/contract/", "tests/acceptance/"],
    }
    async with mcp_session(app, WORKER_TOKEN) as session:
        answer = payload(await session.call_tool("repo.present", args))
    assert answer == {"present": ["tests/contract/"]}
    assert [c[0] for c in backends.github_calls[-2:]] == [
        "/repos/example/target/contents/tests/contract",
        "/repos/example/target/contents/tests/acceptance",
    ]

    for token in (PM_TOKEN, AGENT_TOKEN):
        async with mcp_session(app, token) as session:
            result = await session.call_tool("repo.present", args)
        assert result.isError, "a company role read the base ref through the platform"


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
                "git.merge_pr",
                {"run_id": RUN_ID, "repo": REPO, "number": 5, "sha": HEAD_SHA},
            )
        )
    assert merged == {"merged": True, "sha": "abc123"}
    path, sent, _ = backends.github_calls[-1]
    assert path.endswith("/pulls/5/merge")
    assert sent == {"merge_method": "merge", "sha": HEAD_SHA}

    for token in (PM_TOKEN, AGENT_TOKEN):
        async with mcp_session(app, token) as session:
            result = await session.call_tool(
                "git.merge_pr",
                {"run_id": RUN_ID, "repo": REPO, "number": 5, "sha": HEAD_SHA},
            )
        assert result.isError, "a company role reached git.merge_pr"
    denies = [
        e
        for e in audit_events(audit)
        if e["tool"] == "git.merge_pr" and e["decision"] == "deny"
    ]
    assert len(denies) == 2


async def test_a_moved_head_merges_nothing(app, backends):
    """The merge names the reviewed head; GitHub's 409 for a head that
    moved since is a refusal the platform reads, not a merge."""
    async with mcp_session(app, WORKER_TOKEN) as session:
        refused = payload(
            await session.call_tool(
                "git.merge_pr",
                {"run_id": RUN_ID, "repo": REPO, "number": 5, "sha": "0ld5ha"},
            )
        )
    assert refused["merged"] is False
    assert "head moved after review" in refused["refused"]
    assert backends.github_calls[-1][1]["sha"] == "0ld5ha"


# -- issues.close and git.close_pr (platform only) ----------------------------


@pytest.mark.parametrize(
    ("tool", "number", "route", "body"),
    [
        (
            "issues.close",
            7,
            "/issues/7",
            {"state": "closed", "state_reason": "completed"},
        ),
        ("git.close_pr", 5, "/pulls/5", {"state": "closed"}),
    ],
)
async def test_closing_is_platform_only_and_owes_a_rationale(
    app, backends, audit, tool, number, route, body
):
    """The platform closes with its reason on the audit row; a call
    without one is malformed before policy; no company role reaches it."""
    args = {"run_id": RUN_ID, "repo": REPO, "number": number}
    async with mcp_session(app, WORKER_TOKEN) as session:
        closed = payload(
            await session.call_tool(tool, {**args, "rationale": "merged #5 (abc)"})
        )
        assert closed["state"] == "closed"
        path, sent, _ = backends.github_calls[-1]
        assert path.endswith(route) and sent == body
        refused = await session.call_tool(tool, args)
        assert refused.isError and "rationale" in refused.content[0].text
    (allowed,) = audit_events(audit)
    assert allowed["decision"] == "allow"
    assert allowed["rationale"] == "merged #5 (abc)"

    for token in (PM_TOKEN, AGENT_TOKEN):
        async with mcp_session(app, token) as session:
            result = await session.call_tool(tool, {**args, "rationale": "r"})
        assert result.isError and "default_deny" in result.content[0].text
    assert [e["decision"] for e in audit_events(audit)] == ["allow", "deny", "deny"]
