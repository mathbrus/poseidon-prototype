"""Unit tests for the review activities: the PR read and its refusals,
journaled verdicts, reviewer independence, the merge decision, the
rework loop, and the post-gate merge as a gateway call. Sequencing — the
merge strictly after the gate — is a workflow concern, proven live by
the kind suite."""

import asyncio
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from common.audit import (
    REVIEW_CONTRACT_EDIT,
    REVIEW_READ_PR,
    REVIEW_VERDICT,
    AuditEvent,
    gate_event,
)
from common.policy import GATE_RESOLVE, MERGE_RESOLVE
from common.topology import PLATFORM_NAMESPACE
from common.workflows import MERGE_GATE
from orchestrator.activities.review import ReviewActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryAuditStore,
    InMemoryCompanies,
    InMemoryRunParticipants,
    InMemoryRuns,
)

RUN_ID = "run-review-0001"

REVIEW_SPEC = {
    "namespace": "hello-saas",
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "heartbeat": {"conductor": "pm", "max_attempts": 2},
    "workflows": [
        {"name": "implement", "crew": "feature-delivery", "max_verify_retries": 2},
        {
            "name": "review",
            "crew": "feature-review",
            "gates": {"merge": {"resolved_by": "human"}},
        },
    ],
}

PM_ROW = {
    "agent_id": "pm-1",
    "company": "hello-saas",
    "standing_name": "pm",
    "role": "pm",
    "namespace": "hello-saas",
}


class FakeInstances:
    def __init__(self, rows):
        self.rows = rows

    def for_company(self, company):
        return [r for r in self.rows if r["company"] == company]


def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        opa_url="http://opa.test:8181",
        platform_namespace=PLATFORM_NAMESPACE,
        sandbox_image="poseidon-sandbox:abc123",
        agent_image="poseidon-agent-host:abc123",
        model="mock-brain",
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        temporal_address="temporal.test:7233",
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
    )


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def _activities(settings, spec, instances, audit=None, runs=None, participants=None):
    companies = InMemoryCompanies()
    if spec is not None:
        companies.upsert("hello-saas", spec)
    return ReviewActivities(
        settings,
        runs or InMemoryRuns(),
        companies,
        audit or InMemoryAuditStore(),
        participants or InMemoryRunParticipants(),
    )


def test_verdicts_are_journaled_audit_events(settings, audit):
    """The reviewer's structured verdict becomes an append-only
    process-lifecycle row — the heartbeat.decide idiom: pass maps to allow,
    fail to deny, the rule names the verdict, and the reviewer's own reason
    rides the rationale column in cleartext."""
    runs = InMemoryRuns()
    runs.start(RUN_ID, "review pull request #41", "review", company="hello-saas")
    activities = _activities(
        settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit, runs=runs
    )
    asyncio.run(
        activities.record_verdict(
            RUN_ID, "pass", "Suite green; in scope.", f"review-{RUN_ID}"
        )
    )
    # The conclusion also lands on the row — the fact the consumption
    # rule reads, so the PR stays consumed whatever the gate decides.
    assert runs.get(RUN_ID).verdict == "pass"
    asyncio.run(
        activities.record_verdict(
            RUN_ID, "fail", "Blocking: criteria unmet.", f"review-{RUN_ID}"
        )
    )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.decision, e.rule, e.rationale) for e in events] == [
        ("allow", "verdict_pass", "Suite green; in scope."),
        ("deny", "verdict_fail", "Blocking: criteria unmet."),
    ]
    assert all(e.source == "process-lifecycle" for e in events)
    assert all(e.tool == REVIEW_VERDICT for e in events)
    assert all(e.workflow == f"review-{RUN_ID}" for e in events)


def test_read_pr_keeps_the_shape_and_drops_the_patches(
    settings, monkeypatch, fake_gateway
):
    import orchestrator.activities.review as module

    async def read_pr(self, run_id, repo, number):
        return {
            "number": number,
            "title": "Add slugify",
            "state": "open",
            "head": "feature/slugify-x",
            "head_sha": "f00d5ha",
            "url": "https://pr/41",
            "files": [{"path": "slugify.py", "patch": "+..."}],
        }

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_pr=read_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    pr = asyncio.run(activities.read_pr(RUN_ID, 41))
    assert pr["head_sha"] == "f00d5ha"
    assert "files" not in pr
    # The changed paths survive the shaping: the merge-time class check
    # resolves against them.
    assert pr["paths"] == ["slugify.py"]
    # No contract tree in play: no deletion facts to surface.
    assert pr["contract_deletions"] is None


def test_contract_tree_is_what_the_base_ref_holds(settings, monkeypatch, fake_gateway):
    """The declared contract paths are asked of the git host at base_ref,
    under the platform identity; what comes back is the tree the composed
    verify replays."""
    import orchestrator.activities.review as module

    asked: list[tuple[str, str, list[str]]] = []

    async def present_paths(self, run_id, repo, ref, paths):
        asked.append((repo, ref, list(paths)))
        return [p for p in paths if p == "tests/contract/"]

    monkeypatch.setattr(
        module, "GatewayClient", fake_gateway(present_paths=present_paths)
    )
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    tree = asyncio.run(
        activities.contract_tree(RUN_ID, ["tests/contract/", "tests/acceptance/"])
    )
    assert tree == ["tests/contract/"]
    assert len(asked) == 1 and asked[0][1:] == (
        "main",
        ["tests/contract/", "tests/acceptance/"],
    )


def test_contract_deletions_counts_straight_off_the_patch():
    """The model-free deletion facts: test functions and assertions
    removed/added on the contract tree, plain string counting — diff
    headers (`---`/`+++`) never counted as removals/additions."""
    from orchestrator.activities.review import contract_deletions

    files = [
        {
            "path": "tests/contract/test_api.py",
            "patch": (
                "--- a/tests/contract/test_api.py\n"
                "+++ b/tests/contract/test_api.py\n"
                "-def test_shortens_urls():\n"
                "-    assert shorten('x') == 'y'\n"
                "-    assert resolve('y') == 'x'\n"
                "+async def test_shortens_urls():\n"
                "+    assert True\n"
                " context line\n"
            ),
        },
        # Outside the contract tree: never counted, whatever it deletes.
        {"path": "tests/unit/test_x.py", "patch": "-def test_gone():\n-    assert 1\n"},
    ]
    assert contract_deletions(files, ["tests/contract/"]) == {
        "removed_tests": 1,
        "added_tests": 1,
        "removed_asserts": 2,
        "added_asserts": 1,
    }


def test_contract_deletions_is_none_when_the_tree_is_untouched():
    from orchestrator.activities.review import contract_deletions

    files = [{"path": "slugify.py", "patch": "-assert False\n"}]
    assert contract_deletions(files, ["tests/contract/"]) is None
    assert contract_deletions(files, []) is None


def test_warn_contract_edit_posts_and_audits(settings, audit, monkeypatch, fake_gateway):
    """Both channels at once: the platform comment the human reads before
    resolving the merge signal, and the audit row carrying the counts."""
    import orchestrator.activities.review as module

    comments: list[tuple[int, str]] = []

    async def comment_pr(self, run_id, repo, number, body):
        comments.append((number, body))
        return {"url": "https://pr/41#c-9"}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(comment_pr=comment_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    deletions = {
        "removed_tests": 2,
        "added_tests": 0,
        "removed_asserts": 5,
        "added_asserts": 1,
    }
    asyncio.run(activities.warn_contract_edit(RUN_ID, 41, deletions, "wf-rev"))

    (number, body) = comments[0]
    assert number == 41
    assert "Contract tree edited" in body
    assert "−2" in body and "−5" in body
    (event,) = [e for e in audit.events if e.tool == REVIEW_CONTRACT_EDIT]
    assert "removed_tests=2" in event.rule
    assert event.source == "process-lifecycle"


def test_read_pr_refuses_a_headless_pr(settings, monkeypatch, fake_gateway):
    """No head commit means nothing to provision the review sandbox from
    — refuse before provisioning, like every config activity."""
    import orchestrator.activities.review as module

    async def read_pr(self, run_id, repo, number):
        return {"number": number, "state": "open", "head": "f", "head_sha": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_pr=read_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="no head commit"):
        asyncio.run(activities.read_pr(RUN_ID, 41))


@pytest.mark.parametrize(
    ("pr", "settled"),
    [
        ({"state": "closed", "merged": True}, "merged"),
        ({"state": "closed", "merged": False}, "closed"),
    ],
)
def test_read_pr_refuses_a_settled_pr(
    settings, audit, monkeypatch, fake_gateway, pr, settled
):
    """A merged or closed PR is refused at the fetch step with the named
    rule, non-retryable and journaled in the run's story — before a
    sandbox is provisioned from a head ref the world may have deleted.
    The git host's word decides, not the head ref's presence: a merged PR
    whose branch still exists is just as settled."""
    import orchestrator.activities.review as module

    async def read_pr(self, run_id, repo, number):
        return {"number": number, "head": "feature/slugify-x", **pr}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_pr=read_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(activities.read_pr(RUN_ID, 41))
    assert caught.value.non_retryable
    assert str(caught.value).startswith(f"pr_not_open: pull request #41 is {settled}")
    (event,) = [e for e in audit.events if e.run_id == RUN_ID]
    assert (event.tool, event.decision, event.rule) == (
        REVIEW_READ_PR,
        "deny",
        "pr_not_open",
    )
    assert event.source == "process-lifecycle"
    assert event.workflow == f"review-{RUN_ID}"


def test_merge_pr_raises_when_the_merge_does_not_land(
    settings, monkeypatch, fake_gateway
):
    import orchestrator.activities.review as module

    merged: list[dict[str, Any]] = [
        {"merged": True, "sha": "abc123"},
        {"merged": False, "sha": ""},
    ]
    named: list[str] = []

    async def merge_pr(self, run_id, repo, number, sha, method="merge"):
        named.append(sha)
        return merged.pop(0)

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(merge_pr=merge_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    assert asyncio.run(activities.merge_pr(RUN_ID, 41, "f00d5ha"))["sha"] == "abc123"
    with pytest.raises(RuntimeError, match="did not land"):
        asyncio.run(activities.merge_pr(RUN_ID, 41, "f00d5ha"))
    assert named == ["f00d5ha", "f00d5ha"]


def test_a_moved_head_fails_the_merge_for_good(settings, monkeypatch, fake_gateway):
    """The host refused the pinned merge because the head moved: the
    refusal is the run's error, and no retry can bring the head back."""
    import orchestrator.activities.review as module

    async def merge_pr(self, run_id, repo, number, sha, method="merge"):
        return {"merged": False, "sha": "", "refused": "head moved after review"}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(merge_pr=merge_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    with pytest.raises(ApplicationError, match="head moved after review") as caught:
        asyncio.run(activities.merge_pr(RUN_ID, 41, "0ld5ha"))
    assert caught.value.non_retryable


# -- reviewer independence --------------------------------------------------


def _lineage(runs, participants, *, implement_agents, review_agents):
    """An implement run that delivered PR #41, a review run for it, and
    both runs' identity lineages (already revoked, as teardown leaves
    them)."""
    runs.start("run-impl", "implement issue #7", "implement", "wf-impl", "hello-saas")
    runs.finish("run-impl", "done", pr_url="https://git.test/repo/pulls/41")
    runs.start(RUN_ID, "review pull request #41", "review", "wf-rev", "hello-saas")
    for agent_id in implement_agents:
        participants.add(agent_id, "run-impl", "pm-1", "feature-delivery", "implementer")
    for agent_id in review_agents:
        participants.add(agent_id, RUN_ID, "pm-1", "feature-review", "reviewer")
    participants.terminate_for_run("run-impl")


def test_disjoint_lineages_are_independent(settings):
    runs, participants = InMemoryRuns(), InMemoryRunParticipants()
    _lineage(
        runs,
        participants,
        implement_agents=["exec-impl-1"],
        review_agents=["rev-1"],
    )
    activities = _activities(
        settings,
        REVIEW_SPEC,
        FakeInstances([PM_ROW]),
        runs=runs,
        participants=participants,
    )
    result = asyncio.run(activities.reviewer_independent(RUN_ID, 41))
    assert result["independent"] is True
    assert "run-impl" in result["reason"]


def test_the_delivery_crew_may_not_review_its_own_change(settings):
    """A blueprint may legally bind review to the delivery crew; the
    independence fact refuses to call that independent — the crew that
    wrote the change is grading its own homework, whatever the run ids
    say."""
    runs, participants = InMemoryRuns(), InMemoryRunParticipants()
    runs.start("run-impl", "implement issue #7", "implement", "wf-impl", "hello-saas")
    runs.finish("run-impl", "done", pr_url="https://git.test/repo/pulls/41")
    runs.start(RUN_ID, "review pull request #41", "review", "wf-rev", "hello-saas")
    participants.add("exec-impl-1", "run-impl", "pm-1", "feature-delivery", "implementer")
    participants.terminate_for_run("run-impl")
    participants.add("rev-1", RUN_ID, "pm-1", "feature-delivery", "reviewer")
    activities = _activities(
        settings,
        REVIEW_SPEC,
        FakeInstances([PM_ROW]),
        runs=runs,
        participants=participants,
    )
    result = asyncio.run(activities.reviewer_independent(RUN_ID, 41))
    assert result["independent"] is False
    assert "feature-delivery" in result["reason"]


def test_an_unattributed_pr_fails_independence_closed(settings):
    runs, participants = InMemoryRuns(), InMemoryRunParticipants()
    runs.start(RUN_ID, "review pull request #41", "review", "wf-rev", "hello-saas")
    participants.add("rev-1", RUN_ID, "pm-1", "feature-review", "reviewer")
    activities = _activities(
        settings,
        REVIEW_SPEC,
        FakeInstances([PM_ROW]),
        runs=runs,
        participants=participants,
    )
    result = asyncio.run(activities.reviewer_independent(RUN_ID, 41))
    assert result["independent"] is False
    assert "no implement run" in result["reason"]


def test_a_review_with_no_bound_reviewer_fails_independence_closed(settings):
    runs, participants = InMemoryRuns(), InMemoryRunParticipants()
    _lineage(runs, participants, implement_agents=["exec-impl-1"], review_agents=[])
    activities = _activities(
        settings,
        REVIEW_SPEC,
        FakeInstances([PM_ROW]),
        runs=runs,
        participants=participants,
    )
    result = asyncio.run(activities.reviewer_independent(RUN_ID, 41))
    assert result["independent"] is False
    assert "no reviewer identity" in result["reason"]


# -- the merge.resolve evaluation ------------------------------------------


def test_resolve_merge_audits_the_decision_either_way(settings, audit, monkeypatch):
    import orchestrator.activities.review as module

    answers = iter(
        [
            {"allow": True, "rule": "merge_resolve_delegated"},
            {"allow": False, "rule": "merge_resolve_undelegated_path"},
        ]
    )
    seen = []

    async def fake_evaluate(opa_url, company, decision_input):
        seen.append(decision_input)
        return next(answers)

    monkeypatch.setattr(module, "evaluate_decision", fake_evaluate)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)

    facts = {
        "verdict": "pass",
        "checks_green": True,
        "reviewer_independent": True,
        "contract_green": True,
    }
    released = asyncio.run(
        activities.resolve_merge(
            RUN_ID, "policy", {**facts, "paths": ["src/a.py"]}, "wf-rev"
        )
    )
    declined = asyncio.run(
        activities.resolve_merge(
            RUN_ID, "policy", {**facts, "paths": ["deploy/x"]}, "wf-rev"
        )
    )
    assert released == {"allow": True, "rule": "merge_resolve_delegated"}
    assert declined == {"allow": False, "rule": "merge_resolve_undelegated_path"}

    # The decision input carries the caller-verified facts, platform-shaped,
    # and names the path being decided.
    assert seen[0]["tool"] == MERGE_RESOLVE
    assert seen[0]["lifecycle"] == "platform"
    assert seen[0]["args"] == {"resolver": "policy", **facts, "paths": ["src/a.py"]}

    events = [e for e in audit.events if e.tool == MERGE_RESOLVE]
    assert [(e.decision, e.rule) for e in events] == [
        ("allow", "merge_resolve_delegated"),
        ("deny", "merge_resolve_undelegated_path"),
    ]
    assert all(e.source == "process-lifecycle" for e in events)


# -- the rework loop --------------------------------------------------------


def test_rework_title_round_trips():
    from orchestrator.activities.tickets import REWORK_TITLE_RE, rework_title

    title = rework_title(2, 41, "Add slugify utility")
    match = REWORK_TITLE_RE.match(title)
    assert match is not None
    assert (int(match.group(1)), int(match.group(2))) == (2, 41)


class FakeIssueGateway:
    """A gateway double for the rework and closing activities: serves
    issues by number and the open ones as the backlog, records created and
    closed issues and closed PRs."""

    def __init__(self, issues=None):
        self.issues = issues or {}
        self.created = []
        self.closed = []
        self.closed_prs = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read_issue(self, run_id, repo, number):
        return self.issues[number]

    async def read_open_issues(self, run_id, repo):
        return [i for i in self.issues.values() if i.get("state") == "open"]

    async def close_issue(self, run_id, repo, number, rationale):
        self.closed.append((number, rationale))
        self.issues[number] = {**self.issues[number], "state": "closed"}
        return self.issues[number]

    async def close_pr(self, run_id, repo, number, rationale):
        self.closed_prs.append((number, rationale))
        return {"number": number, "state": "closed"}

    async def create_issue(self, run_id, repo, title, body, rationale):
        self.created.append({"repo": repo, "title": title, "body": body})
        return {
            "url": f"https://git.test/r/issues/{90 + len(self.created)}",
            "number": 90 + len(self.created),
        }


def _chain(runs, task):
    """An implement run that delivered PR #41 from the given task."""
    runs.start("run-impl", task, "implement", "wf-impl", "hello-saas")
    runs.finish("run-impl", "done", pr_url="https://git.test/r/pulls/41")


def test_rework_depth_is_zero_outside_any_chain(settings, monkeypatch):
    import orchestrator.activities.review as module

    monkeypatch.setattr(module, "GatewayClient", FakeIssueGateway())
    runs = InMemoryRuns()
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    # No implement run at all.
    assert asyncio.run(activities.rework_depth(RUN_ID, 41)) == {
        "depth": 0,
        "root_pr": 41,
    }
    # An implement run from a free-form task names no ticket to read.
    _chain(runs, "the task file text")
    assert asyncio.run(activities.rework_depth(RUN_ID, 41)) == {
        "depth": 0,
        "root_pr": 41,
    }


def test_rework_depth_reads_the_canonical_ticket_title(settings, monkeypatch):
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={7: {"number": 7, "title": "Rework 2 for PR #38: Add slugify"}}
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _chain(runs, "implement issue #7")
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    assert asyncio.run(activities.rework_depth(RUN_ID, 41)) == {
        "depth": 2,
        "root_pr": 38,
    }


def test_rework_depth_treats_a_plain_ticket_as_depth_zero(settings, monkeypatch):
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={7: {"number": 7, "title": "Implement: add slugify"}}
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _chain(runs, "implement issue #7")
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    assert asyncio.run(activities.rework_depth(RUN_ID, 41)) == {
        "depth": 0,
        "root_pr": 41,
    }


def test_file_rework_files_the_canonical_ticket_and_records_it(settings, monkeypatch):
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway()
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    runs.start(RUN_ID, "review pull request #41", "review", "wf-rev", "hello-saas")
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    ticket = asyncio.run(
        activities.file_rework(
            RUN_ID,
            41,
            38,
            3,
            "review verdict failed",
            "Blocking: criteria unmet.",
            "Add slugify utility",
            "https://git.test/r/pulls/41",
            ["slugify.py", "tests/test_slugify.py"],
        )
    )
    (created,) = gateway.created
    assert created["title"] == "Rework 3 for PR #38: Add slugify utility"
    body = created["body"]
    # The ticket is the rework implementer's whole brief.
    assert "review verdict failed" in body
    assert "Blocking: criteria unmet." in body
    assert "- slugify.py" in body
    assert RUN_ID in body  # attributed to the review run
    assert "https://git.test/r/pulls/41" in body
    # And it lands on the run row, the breadcrumb a scope run leaves too.
    assert runs.get(RUN_ID).ticket_urls == [ticket["url"]]


# -- the rework-loop red-team: forging depth cannot extend autonomy ---------


def test_a_forged_standalone_ticket_cannot_reset_a_prs_depth(settings, monkeypatch):
    """The rework-loop bait: an attacker files a ticket titled `Rework 0
    for PR #X` to reset the bound and loop forever. It changes nothing —
    the platform reads depth from the PR's own lineage (the ticket its
    implement run consumed), not from any freely-filed issue. The forged
    ticket the PR never consumed is simply not read."""
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={
            7: {"number": 7, "title": "Rework 2 for PR #38: Add slugify"},
            999: {"number": 999, "title": "Rework 0 for PR #38: reset the counter"},
        }
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _chain(runs, "implement issue #7")  # the PR's real lineage: ticket #7
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    # Depth is read from #7 (depth 2), never from the forged #999.
    assert asyncio.run(activities.rework_depth(RUN_ID, 41)) == {
        "depth": 2,
        "root_pr": 38,
    }


def test_a_forged_higher_depth_only_escalates_sooner(settings, monkeypatch):
    """Even where an attacker could influence the consumed ticket's title,
    the parse can only *shrink* autonomy: a forged high depth trips the
    bound sooner. The workflow denies the merge at depth >= max_reworks —
    proven in test_review_workflow's exhausted-chain test; here we pin that
    the depth read is monotonic in the title, never capped or wrapped."""
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={7: {"number": 7, "title": "Rework 99 for PR #38: Add slugify"}}
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _chain(runs, "implement issue #7")
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    assert asyncio.run(activities.rework_depth(RUN_ID, 41))["depth"] == 99


# -- the recorded human verdict ---------------------------------------------


def _console_verdict(run_id: str, approved: bool, principal: str, ts: str) -> AuditEvent:
    return AuditEvent(
        ts=ts,
        run_id=run_id,
        agent_id=principal,
        role="human",
        tool=GATE_RESOLVE,
        args_digest="d",
        decision="allow",
        rule=f"gate_resolve_merge_{'approve' if approved else 'reject'}",
        latency_ms=0,
        backend_status=None,
        source="console",
        company="hello-saas",
        lifecycle="human",
    )


def test_read_human_verdict_is_none_until_the_console_answered(settings, audit):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    # A process-gate row (the workflow's own record of a signal) is not a
    # verdict: only the console's rows name a person.
    audit.append(
        AuditEvent(
            ts="2026-01-01T00:00:00+00:00",
            run_id=RUN_ID,
            agent_id="orchestrator",
            role="platform",
            tool=gate_event(MERGE_GATE),
            args_digest="d",
            decision="allow",
            rule="gate_approved",
            latency_ms=0,
            backend_status=None,
            source="process-gate",
        )
    )
    assert asyncio.run(activities.read_human_verdict(RUN_ID)) is None


def test_read_human_verdict_reads_the_latest_console_row(settings, audit):
    """Latest wins: an approving verdict followed by a rejecting one reads as rejected,
    and another run's verdict is never this run's."""
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    audit.append(_console_verdict(RUN_ID, True, "owner", "2026-01-01T00:00:01+00:00"))
    audit.append(
        _console_verdict("run-other", True, "owner", "2026-01-01T00:00:03+00:00")
    )
    approved = asyncio.run(activities.read_human_verdict(RUN_ID))
    assert approved == {
        "principal": "owner",
        "approved": True,
        "ts": "2026-01-01T00:00:01+00:00",
    }
    audit.append(_console_verdict(RUN_ID, False, "owner", "2026-01-01T00:00:02+00:00"))
    rejected = asyncio.run(activities.read_human_verdict(RUN_ID))
    assert rejected["approved"] is False


def test_resolve_merge_on_the_human_path_carries_only_the_verdict(
    settings, audit, monkeypatch
):
    import orchestrator.activities.review as module

    seen = []

    async def fake_evaluate(opa_url, company, decision_input):
        seen.append(decision_input)
        return {"allow": True, "rule": "merge_resolve_human"}

    monkeypatch.setattr(module, "evaluate_decision", fake_evaluate)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    verdict = {"principal": "owner", "approved": True, "ts": "t"}
    result = asyncio.run(
        activities.resolve_merge(RUN_ID, "human", {"human_verdict": verdict}, "wf-rev")
    )
    assert result == {"allow": True, "rule": "merge_resolve_human"}
    assert seen[0]["args"] == {"resolver": "human", "human_verdict": verdict}


# -- closing what a merge fulfilled, and what a rework superseded -------------


def _delivered(runs, run_id, task, pr):
    runs.start(run_id, task, "implement", f"wf-{run_id}", "hello-saas")
    runs.finish(run_id, "done", pr_url=f"https://git.test/r/pull/{pr}")


def _issue(number, title, state="open"):
    return {"number": number, "title": title, "state": state}


def test_close_tickets_closes_the_consumed_ticket(settings, monkeypatch):
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={7: _issue(7, "Add slugify"), 8: _issue(8, "Unrelated")}
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _delivered(runs, "run-impl", "implement issue #7", 41)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    closed = asyncio.run(activities.close_tickets(RUN_ID, 41, "abc123", "Add slugify"))
    assert closed == [7]
    assert gateway.closed == [(7, "merged pull request #41 (abc123): Add slugify")]


def test_a_merged_rework_closes_the_whole_chain(settings, monkeypatch):
    """Root ticket #7 delivered PR #41, which failed review twice: rework
    tickets #90 (PR #42) and #91 (PR #43). PR #43 merging closes all three;
    a rework filed for another root stays open."""
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(
        issues={
            7: _issue(7, "Add slugify"),
            90: _issue(90, "Rework 1 for PR #41: Add slugify"),
            91: _issue(91, "Rework 2 for PR #41: Add slugify"),
            92: _issue(92, "Rework 1 for PR #50: Other"),
        }
    )
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _delivered(runs, "run-a", "implement issue #7", 41)
    _delivered(runs, "run-b", "implement issue #90", 42)
    _delivered(runs, "run-c", "implement issue #91", 43)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    assert asyncio.run(activities.close_tickets(RUN_ID, 43, "s", "t")) == [7, 90, 91]
    assert gateway.issues[92]["state"] == "open"


def test_close_tickets_skips_closed_tickets_and_unattributed_prs(settings, monkeypatch):
    """A ticket a human already closed is read and left alone; a PR no
    implement run delivered, or one delivered from a free-form task, names
    no ticket and closes nothing."""
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(issues={7: _issue(7, "Add slugify", state="closed")})
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    assert asyncio.run(activities.close_tickets(RUN_ID, 41, "s", "t")) == []
    _delivered(runs, "run-free", "the task file text", 41)
    assert asyncio.run(activities.close_tickets(RUN_ID, 41, "s", "t")) == []
    _delivered(runs, "run-impl", "implement issue #7", 42)
    assert asyncio.run(activities.close_tickets(RUN_ID, 42, "s", "t")) == []
    assert gateway.closed == []


@pytest.mark.parametrize(("merged", "closes"), [(True, [7]), (False, [])])
def test_read_pr_closes_the_tickets_of_a_pr_merged_by_hand(
    settings, monkeypatch, merged, closes
):
    """A review asked for a PR a human already merged learns of the merge
    first: the tickets close once, on that transition, and the settle is
    remembered. A PR closed unmerged leaves its ticket to the human."""
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway(issues={7: _issue(7, "Add slugify")})

    async def read_pr(run_id, repo, number):
        return {"number": number, "state": "closed", "merged": merged, "head": "f"}

    gateway.read_pr = read_pr
    monkeypatch.setattr(module, "GatewayClient", gateway)
    runs = InMemoryRuns()
    _delivered(runs, "run-impl", "implement issue #7", 41)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    for _ in range(2):
        with pytest.raises(ApplicationError, match="pr_not_open"):
            asyncio.run(activities.read_pr(RUN_ID, 41))
    assert [n for n, _ in gateway.closed] == closes
    assert all(
        r == "pull request #41 merged outside the platform" for _, r in gateway.closed
    )
    assert runs.get("run-impl").pr_settled == ("merged" if merged else "closed")


def test_close_superseded_pr_names_the_rework_ticket(settings, monkeypatch):
    import orchestrator.activities.review as module

    gateway = FakeIssueGateway()
    monkeypatch.setattr(module, "GatewayClient", gateway)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    asyncio.run(activities.close_superseded_pr(RUN_ID, 41, 91))
    assert gateway.closed_prs == [(41, "superseded by rework ticket #91")]
