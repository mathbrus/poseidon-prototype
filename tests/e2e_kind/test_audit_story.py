"""E4 on the cluster (plan step 6): the whole story of a run is
reconstructable by `run_id` alone — every enforcement decision the platform
made, in order, from one query. Since plan step 8 that is literally one
query: the gateway and the egress proxy both write the registry
`audit_events` table.
"""

from .conftest import audit_story


def _executor_id(run_id: str) -> str:
    from orchestrator.identity import ephemeral_agent_id

    return ephemeral_agent_id("feature-delivery", "executor", run_id)


def test_story_covers_the_whole_run(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    assert story, "no audit events for the run"

    tools = {e["tool"] for e in story}
    # The run's arc since step 19: sandbox registered, workspace fetched,
    # the crew stood up (request recorded under the conductor's authority,
    # members minted, the executor's pod provisioned, fulfillment), files
    # written, tests run, work committed, bundle pulled, PR opened, and the
    # crew revoked at teardown.
    assert {
        "/admin/sandboxes",
        "repo.archive",
        "lifecycle.record_request",
        "lifecycle.mint",
        "lifecycle.provision_pod",
        "lifecycle.fulfill",
        "sandbox.write_file",
        "sandbox.exec",
        "sandbox.run_tests",
        "sandbox.git_bundle",
        "git.open_pr",
        "lifecycle.revoke",
    } <= tools, f"story is missing steps: {sorted(tools)}"


def test_delivery_never_precedes_the_platform_verify(implement_run: dict) -> None:
    """The step-19 gate of record, in the story's own ordering: the
    platform's green sandbox.run_tests (the orchestrator's platform
    lifecycle) comes before the PR ever opens — delivery is released by
    the gate, not by the executor's claim."""
    story = audit_story(implement_run["run_id"])
    platform_verifies = [
        i for i, e in enumerate(story)
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert platform_verifies, "no platform-identity verify in the story"
    pr_opens = [i for i, e in enumerate(story) if e["tool"] == "git.open_pr"]
    assert pr_opens, "no delivery in the story"
    assert min(platform_verifies) < min(pr_opens), (
        "delivery happened before the platform verify gate"
    )


def test_every_event_is_attributed_and_decided(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    for event in story:
        assert event["run_id"] == implement_run["run_id"]
        assert event["decision"] in ("allow", "deny")
        assert event["agent_id"]
        assert event["ts"]


def test_happy_path_story_contains_no_denies(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    denies = [e for e in story if e["decision"] == "deny"]
    assert denies == [], f"unexpected denies in the happy path: {denies}"


def _seed_event(run_id: str, source: str, agent_id: str, tool: str,
                decision: str, rule: str) -> None:
    """Seed an event through the v2 ingestion contract (the same table and
    columns every producer writes). The llm-gateway and process-gate
    producers land at plan steps 14 and 18–20; the *contract* — one shape,
    one table, one query — is step 13's, and this is its proof."""
    import subprocess

    from .conftest import PLATFORM_NS

    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "exec", "-i", "statefulset/postgres", "--",
         "psql", "-U", "poseidon", "-d", "poseidon", "-At",
         "-v", f"run_id={run_id}", "-v", f"source={source}",
         "-v", f"agent_id={agent_id}", "-v", f"tool={tool}",
         "-v", f"decision={decision}", "-v", f"rule={rule}",
         "-f", "-"],
        input=(
            "INSERT INTO registry.audit_events"
            " (ts, run_id, agent_id, role, tool, args_digest, decision, rule,"
            "  latency_ms, source, company, lifecycle, workflow)"
            " VALUES (now(), :'run_id', :'agent_id', '', :'tool',"
            "  repeat('0', 64), :'decision', :'rule', 0, :'source',"
            "  'hello-saas', 'standing', 'implement')"
        ),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_one_query_spans_all_five_sources(implement_run: dict) -> None:
    """The step-13 done-when, grown with the run's real producers: one
    run_id-correlated query returns every source's events."""
    run_id = implement_run["run_id"]
    _seed_event(run_id, "process-gate", "orchestrator",
                "gate.merge", "allow", "gate_approved")
    _seed_event(run_id, "sandbox-infra", "egress-proxy",
                "sandbox.egress", "deny", "egress_not_allowlisted")

    story = audit_story(run_id)
    # gateway, llm-gateway and process-lifecycle events are the real run's
    # (steps 12/14/16); the remaining two sources are seeded through the
    # same ingestion contract (their producers land at step 20).
    assert {e["source"] for e in story} == {
        "gateway", "llm-gateway", "process-lifecycle",
        "process-gate", "sandbox-infra",
    }
    assert all(e["run_id"] == run_id for e in story)


def test_gateway_events_carry_v2_attribution(implement_run: dict) -> None:
    """Since step 13 the real pipeline emits v2: every gateway event names
    its source, and company-caller events carry company + lifecycle."""
    run_id = implement_run["run_id"]
    story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
    assert story
    for event in story:
        assert event["lifecycle"] in ("standing", "ephemeral", "platform")
    executor_events = [e for e in story if e["agent_id"] == _executor_id(run_id)]
    assert executor_events
    for event in executor_events:
        assert event["company"] == "hello-saas"
        assert event["lifecycle"] == "ephemeral"
        assert event["parent_agent_id"] == "pm-1"
        assert event["workflow"] == "implement"
