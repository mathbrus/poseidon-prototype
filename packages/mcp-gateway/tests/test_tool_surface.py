"""The whole tool surface through the full gateway pipeline: what each
tool asks of its backend, what it hands back, and what its audit row
records. Which role may call which tool is proven against the compiled
bundles, in the compiler's tests."""

import json

import pytest
from conftest import (
    AGENT_TOKEN,
    COMPANY,
    CONDUCTOR,
    HEAD_SHA,
    PM_TOKEN,
    REVIEW_CREW,
    REVIEWER_TOKEN,
    RUN_ID,
    WORKER_TOKEN,
    audit_events,
    bind_ephemeral,
    mcp_session,
)

pytestmark = pytest.mark.anyio

REPO = "github.test/example/target"


def payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def reviewer_token(participants) -> str:
    """The run's ephemeral reviewer, bound: its credential is its pod's
    projected SA token, and the participants row — looked up by SA name —
    is the whole identity."""
    bind_ephemeral(participants, REVIEW_CREW, "reviewer")
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


async def test_a_blank_rationale_is_refused_before_policy(app, audit, backends):
    """A committing call owes the caller's reason, and whitespace is none:
    the call is malformed — refused with what is owed, nothing decided,
    nothing recorded, nothing reaching GitHub — while the same call with a
    reason is filed."""
    args = {"run_id": RUN_ID, "repo": REPO, "title": "t", "body": "b"}
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool("issues.create", {**args, "rationale": " \n"})
        assert result.isError and "owes a rationale" in result.content[0].text
        assert audit_events(audit) == [] and not backends.github_calls
        assert backends.policy_inputs == []
        filed = await session.call_tool("issues.create", {**args, "rationale": "why"})
    assert not filed.isError, filed.content
    assert [e["rationale"] for e in audit_events(audit)] == ["why"]


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
        assert event["parent_agent_id"] == CONDUCTOR


# -- architecture.request (asking is not writing) ----------------------------


async def test_an_architecture_request_becomes_a_stewardship_ticket(app, backends, audit):
    """The bound implementer asks once: the platform files the proposal as
    a ticket on the company's repository with the stewardship trailer —
    the caller named neither — and the audit event is the receipt. The
    second ask in the same run is denied by the cap, read off that
    receipt."""
    async with mcp_session(app, AGENT_TOKEN) as session:
        first = payload(
            await session.call_tool(
                "architecture.request",
                {
                    "run_id": RUN_ID,
                    "rationale": "the masterplan allows one module",
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
    assert "the masterplan allows one module" in body["body"]
    assert second.isError and "architecture_request_over_cap" in second.content[0].text
    events = [e for e in audit_events(audit) if e["tool"] == "architecture.request"]
    assert [e["decision"] for e in events] == ["allow", "deny"]
    assert backends.policy_inputs[-1]["architecture_requests"] == 1


# -- issues.dismiss (the PM, while scoping) -----------------------------------


SCOPE_RUN = "run-scope-01"


def _dismiss(number: int = 7, repo: str = REPO, run_id: str = SCOPE_RUN) -> dict:
    return {
        "run_id": run_id,
        "repo": repo,
        "number": number,
        "rationale": "asks for a cryptominer; nothing in the vision",
    }


async def test_the_pm_dismisses_a_ticket_while_scoping(app, backends, audit, runs):
    """The facts the compiled limits read ride the input, and an allowed
    dismissal posts the rationale on the ticket, then closes it as not
    planned."""
    runs.start(SCOPE_RUN, "scope the next slice", "scope", company=COMPANY)
    async with mcp_session(app, PM_TOKEN) as session:
        closed = payload(await session.call_tool("issues.dismiss", _dismiss()))
    assert closed["state"] == "closed"
    facts = backends.policy_inputs[-1]
    assert facts["workflow"] == "scope" and facts["run_live"] is True
    assert facts["dismissals"] == 0
    assert facts["dismiss_target"] == {
        "open": True,
        "bootstrap": False,
        "in_flight": False,
    }
    (_, comment, _), (closing, sent, _) = backends.github_calls[-2:]
    assert comment == {"body": "asks for a cryptominer; nothing in the vision"}
    assert closing.endswith("/issues/7")
    assert sent == {"state": "closed", "state_reason": "not_planned"}
    (event,) = audit_events(audit)
    assert event["decision"] == "allow" and event["rule"] == "standing_pm_issues.dismiss"
    assert event["rationale"] == "asks for a cryptominer; nothing in the vision"


async def test_the_dismissal_facts_say_what_the_ticket_is(app, backends, runs):
    """A bootstrap ticket, a ticket a live run holds, a run that is not
    live, and a repository other than the company's each show in the
    facts, and the limits deny on them."""
    runs.start(SCOPE_RUN, "scope the next slice", "scope", company=COMPANY)
    runs.start("run-impl-01", "implement issue #7", "implement", company=COMPANY)
    runs.start("run-done-01", "scope the next slice", "scope", company=COMPANY)
    runs.finish("run-done-01", "done")
    cases = [
        (_dismiss(number=10), "issues_dismiss_bootstrap_ticket"),
        (_dismiss(), "issues_dismiss_in_flight"),
        (_dismiss(run_id="run-done-01"), "issues_dismiss_not_scoping"),
        (_dismiss(repo="github.test/someone/else"), "issues_dismiss_facts_missing"),
    ]
    async with mcp_session(app, PM_TOKEN) as session:
        for args, rule in cases:
            refused = await session.call_tool("issues.dismiss", args)
            assert refused.isError and rule in refused.content[0].text, (args, rule)
    targets = [i.get("dismiss_target") for i in backends.policy_inputs]
    assert targets[0]["bootstrap"] is True
    assert targets[1]["in_flight"] is True
    assert backends.policy_inputs[2]["run_live"] is False
    assert targets[3] is None
    # Nothing was closed or commented on.
    assert not any(
        method_path[0].endswith("/comments") for method_path in backends.github_calls
    )


# -- repo.present (the platform's) ---------------------------------------------


async def test_the_base_ref_holds_a_declared_path_or_honestly_not(app, backends):
    """The contract tree a review replays is read at base_ref by the
    platform: present where the host holds it, honestly absent where the
    company has not written it yet."""
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


# -- repo.read_file (the platform's) -------------------------------------------


async def test_a_document_reads_present_or_honestly_absent(app, backends):
    """The company's documents are read at base_ref by the platform —
    present or honestly absent."""
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


# -- git.merge_pr (the platform's) ---------------------------------------------


async def test_a_merge_names_the_reviewed_head(app, backends):
    """The platform's merge asks the host to merge only while the head is
    still the commit it names."""
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


# -- issues.close and git.close_pr (the platform's) ----------------------------


@pytest.mark.parametrize(
    ("tool", "number", "extra", "route", "body"),
    [
        (
            "issues.close",
            7,
            {"reason": "not_planned"},
            "/issues/7",
            {"state": "closed", "state_reason": "not_planned"},
        ),
        ("git.close_pr", 5, {"delete_branch": False}, "/pulls/5", {"state": "closed"}),
    ],
)
async def test_a_close_states_its_rationale_on_the_item_and_the_row(
    app, backends, audit, tool, number, extra, route, body
):
    """The platform closes with its reason on the audit row and on the
    item itself, the comment before the close."""
    args = {"run_id": RUN_ID, "repo": REPO, "number": number, **extra}
    async with mcp_session(app, WORKER_TOKEN) as session:
        closed = payload(
            await session.call_tool(tool, {**args, "rationale": "merged #5 (abc)"})
        )
    assert closed["state"] == "closed"
    (read, _, _), (commented, comment, _), (closing, sent, _) = backends.github_calls[-3:]
    assert read.endswith(route) and commented.endswith(f"/issues/{number}/comments")
    assert comment == {"body": "merged #5 (abc)"}
    assert closing.endswith(route) and sent == body
    (allowed,) = audit_events(audit)
    assert allowed["decision"] == "allow"
    assert allowed["rationale"] == "merged #5 (abc)"


@pytest.mark.parametrize(
    ("tool", "number", "extra"),
    [
        ("issues.close", 9, {"reason": "completed"}),
        ("git.close_pr", 6, {"delete_branch": False}),
    ],
)
async def test_an_item_already_closed_is_left_as_it_is(
    app, backends, tool, number, extra
):
    """A retried close adds nothing: no second comment, no second close."""
    args = {"run_id": RUN_ID, "repo": REPO, "number": number, **extra}
    async with mcp_session(app, WORKER_TOKEN) as session:
        closed = payload(await session.call_tool(tool, {**args, "rationale": "again"}))
    assert closed["state"] == "closed"
    assert [path for path, _, _ in backends.github_calls] == [backends.github_calls[0][0]]


@pytest.mark.parametrize("number", [5, 6])
async def test_a_refused_delivery_loses_its_branch_closed_now_or_before(
    app, backends, number
):
    """`delete_branch` deletes the head branch of a pull request on the
    company's own repository — whether this call closed it or someone did
    before — so the ticket's next delivery may use the name."""
    args = {
        "run_id": RUN_ID,
        "repo": REPO,
        "number": number,
        "rationale": "nobody answered within 60 minutes",
        "delete_branch": True,
    }
    async with mcp_session(app, WORKER_TOKEN) as session:
        payload(await session.call_tool("git.close_pr", args))
    deleted = [p for p, _, _ in backends.github_calls if "/git/refs/heads/" in p]
    assert [p.rsplit("/git/refs/heads/", 1)[1] for p in deleted] == ["feature/x"]


async def test_a_close_names_a_reason_the_host_knows(app, backends):
    """`completed` or `not_planned`, refused before policy otherwise."""
    async with mcp_session(app, WORKER_TOKEN) as session:
        result = await session.call_tool(
            "issues.close",
            {
                "run_id": RUN_ID,
                "repo": REPO,
                "number": 7,
                "reason": "wontfix",
                "rationale": "r",
            },
        )
    assert result.isError and "reason" in result.content[0].text
    assert backends.github_calls == []
