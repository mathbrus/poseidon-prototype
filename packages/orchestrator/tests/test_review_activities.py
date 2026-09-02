"""Unit tests for the review activities: config resolution
fail-closed, gate outcomes as first-class process-gate audit events, and
the post-gate merge as a gateway call. Sequencing — the merge strictly
after the gate — is a workflow concern, proven live by the kind suite."""

import asyncio
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

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
    "workflows": [
        {
            "name": "implement",
            "enabled": True,
            "conductor": "pm",
            "crew": "feature-delivery",
            "max_verify_retries": 2,
        },
        {
            "name": "review",
            "enabled": True,
            "conductor": "pm",
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
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
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
        instances,
        audit or InMemoryAuditStore(),
        participants or InMemoryRunParticipants(),
    )


def test_review_config_resolves_crew_conductor_and_gate(settings):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    config = asyncio.run(activities.review_config())
    assert config == {
        "company": "hello-saas",
        "crew_template": "feature-review",
        "conductor": "pm-1",
        "conductor_role": "pm",
        "conductor_url": "http://pm.hello-saas.svc.cluster.local:8000",
        "merge_gate": {"resolved_by": "human"},
        "contract_paths": [],
    }


def test_review_config_carries_no_gate_when_none_is_declared(settings):
    spec = {
        "workflows": [
            {
                "name": "review",
                "enabled": True,
                "conductor": "pm",
                "crew": "feature-review",
            },
        ],
    }
    activities = _activities(settings, spec, FakeInstances([PM_ROW]))
    assert asyncio.run(activities.review_config())["merge_gate"] is None


def test_review_config_fails_closed_without_a_blueprint(settings):
    activities = _activities(settings, None, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="no enabled"):
        asyncio.run(activities.review_config())


def test_review_config_fails_closed_without_a_crew(settings):
    spec = {
        "workflows": [
            {"name": "review", "enabled": True, "conductor": "pm", "crew": None}
        ]
    }
    activities = _activities(settings, spec, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="binds no crew"):
        asyncio.run(activities.review_config())


def test_review_config_fails_closed_without_a_conductor_instance(settings):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([]))
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(activities.review_config())


def test_create_review_run_opens_a_review_row(settings):
    runs = InMemoryRuns()
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    asyncio.run(activities.create_review_run(RUN_ID, 41))
    record = runs.get(RUN_ID)
    assert record.status == "running"
    assert record.workflow == "review"
    assert record.workflow_id == f"review-{RUN_ID}"
    assert "#41" in record.task


def test_gate_outcomes_are_process_gate_audit_events(settings, audit):
    """Escalation and the verdict are first-class rows carrying the
    workflow id that maps back to run_id — approved is the only allow."""
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit=audit)
    for kind in ("escalated", "approved"):
        asyncio.run(
            activities.record_gate_outcome(RUN_ID, "merge", kind, f"review-{RUN_ID}")
        )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.rule, e.decision) for e in events] == [
        ("gate_escalated", "deny"),
        ("gate_approved", "allow"),
    ]
    assert all(e.source == "process-gate" for e in events)
    assert all(e.tool == "gate.merge" for e in events)
    assert all(e.workflow == f"review-{RUN_ID}" for e in events)


def test_verdicts_are_journaled_audit_events(settings, audit):
    """The reviewer's structured verdict becomes an append-only
    process-lifecycle row — the heartbeat.decide idiom: pass maps to allow,
    fail to deny, and the one-line summary rides the rule field."""
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
    assert [(e.decision, e.rule) for e in events] == [
        ("allow", "Suite green; in scope."),
        ("deny", "Blocking: criteria unmet."),
    ]
    assert all(e.source == "process-lifecycle" for e in events)
    assert all(e.tool == "review.verdict" for e in events)
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
            "url": "https://pr/41",
            "files": [{"path": "slugify.py", "patch": "+..."}],
        }

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_pr=read_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    pr = asyncio.run(activities.read_pr(RUN_ID, 41))
    assert pr["head"] == "feature/slugify-x"
    assert "files" not in pr
    # The changed paths survive the shaping: the merge-time class check
    # resolves against them.
    assert pr["paths"] == ["slugify.py"]
    # No contract tree in play: no deletion facts to surface.
    assert pr["contract_deletions"] is None


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
    (event,) = [e for e in audit.events if e.tool == "review.contract_edit"]
    assert "removed_tests=2" in event.rule
    assert event.source == "process-lifecycle"


def test_read_pr_refuses_a_headless_pr(settings, monkeypatch, fake_gateway):
    """No head ref means nothing to provision the review sandbox from
    — refuse before provisioning, like every config activity."""
    import orchestrator.activities.review as module

    async def read_pr(self, run_id, repo, number):
        return {"number": number, "state": "open", "head": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_pr=read_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="no head ref"):
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
        "review.read_pr",
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

    async def merge_pr(self, run_id, repo, number, method="merge"):
        return merged.pop(0)

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(merge_pr=merge_pr))
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    assert asyncio.run(activities.merge_pr(RUN_ID, 41))["sha"] == "abc123"
    with pytest.raises(RuntimeError, match="did not land"):
        asyncio.run(activities.merge_pr(RUN_ID, 41))


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

    released = asyncio.run(
        activities.resolve_merge(RUN_ID, "pass", True, True, True, ["src/a.py"], "wf-rev")
    )
    declined = asyncio.run(
        activities.resolve_merge(RUN_ID, "pass", True, True, True, ["deploy/x"], "wf-rev")
    )
    assert released == {"allow": True, "rule": "merge_resolve_delegated"}
    assert declined == {"allow": False, "rule": "merge_resolve_undelegated_path"}

    # The decision input carries the caller-verified facts, platform-shaped.
    assert seen[0]["tool"] == "merge.resolve"
    assert seen[0]["lifecycle"] == "platform"
    assert seen[0]["args"] == {
        "verdict": "pass",
        "checks_green": True,
        "reviewer_independent": True,
        "contract_green": True,
        "paths": ["src/a.py"],
    }

    events = [e for e in audit.events if e.tool == "merge.resolve"]
    assert [(e.decision, e.rule) for e in events] == [
        ("allow", "merge_resolve_delegated"),
        ("deny", "merge_resolve_undelegated_path"),
    ]
    assert all(e.source == "process-lifecycle" for e in events)


def test_policy_gate_outcomes_map_to_decisions(settings, audit):
    """policy_resolved released a merge (allow); policy_declined fell back
    to the human signal (deny) — distinguishable from human approvals
    forever."""
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    for kind in ("policy_resolved", "policy_declined"):
        asyncio.run(
            activities.record_gate_outcome(RUN_ID, "merge", kind, f"review-{RUN_ID}")
        )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.rule, e.decision) for e in events] == [
        ("gate_policy_resolved", "allow"),
        ("gate_policy_declined", "deny"),
    ]


# -- the rework loop --------------------------------------------------------


def test_rework_title_round_trips():
    from orchestrator.activities.review import REWORK_TITLE_RE, rework_title

    title = rework_title(2, 41, "Add slugify utility")
    match = REWORK_TITLE_RE.match(title)
    assert match is not None
    assert (int(match.group(1)), int(match.group(2))) == (2, 41)


class FakeIssueGateway:
    """A gateway double for the rework activities: serves one issue by
    number, records created issues."""

    def __init__(self, issues=None):
        self.issues = issues or {}
        self.created = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read_issue(self, run_id, repo, number):
        return self.issues[number]

    async def create_issue(self, run_id, repo, title, body):
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
    # And it lands on the run row, the intake breadcrumb.
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
