"""S8 flow tests: the §4.1 step sequence with all collaborators mocked."""

import json

import pytest

from runner.flow import execute_run, manifest_path, read_manifest
from conftest import (
    PR_URL,
    RUN_ID,
    SANDBOX_URL,
    FakeAgent,
    FakeBackend,
    FakeGateway,
    write_audit_lines,
)

pytestmark = pytest.mark.anyio

TASK = "Add a greet() function with tests."


# --- happy path ---------------------------------------------------------------


async def test_happy_path_runs_all_steps_in_order(settings):
    backend, gateway, agent = FakeBackend(), FakeGateway(), FakeAgent()

    manifest = await execute_run(settings, TASK, backend, gateway, agent, run_id=RUN_ID)

    assert manifest.status == "done"
    assert manifest.pr_url == PR_URL
    assert manifest.iterations == 2
    assert manifest.error is None

    assert gateway.events == [
        ("fetch_workspace", (settings.target_repo, "main")),
        ("register_sandbox", (RUN_ID, SANDBOX_URL)),
    ]
    assert backend.events == ["provision", "destroy"]
    assert agent.dispatched == [(RUN_ID, TASK)]
    assert agent.polls >= 3  # polled through 'running' to the terminal state


async def test_run_id_is_minted_when_not_supplied(settings):
    manifest = await execute_run(
        settings, TASK, FakeBackend(), FakeGateway(), FakeAgent()
    )
    assert manifest.run_id.startswith("run-")
    assert manifest_path(settings, manifest.run_id).is_file()


# --- manifest contents ----------------------------------------------------------


async def test_manifest_is_written_finalized_and_counts_audit_lines(settings):
    audit_stub = {"tool": "sandbox.exec", "decision": "allow"}
    write_audit_lines(
        settings,
        [
            {**audit_stub, "run_id": RUN_ID},
            {**audit_stub, "run_id": "run-SOMEONE-ELSE"},
            {**audit_stub, "run_id": RUN_ID, "decision": "deny"},
        ],
    )

    manifest = await execute_run(
        settings, TASK, FakeBackend(), FakeGateway(), FakeAgent(), run_id=RUN_ID
    )

    assert manifest.audit_lines == 2  # only this run's events

    on_disk = json.loads(manifest_path(settings, RUN_ID).read_text())
    assert on_disk == manifest.model_dump()
    assert on_disk["task"] == TASK
    assert on_disk["status"] == "done"
    assert on_disk["created_at"] <= on_disk["finished_at"]

    assert read_manifest(settings, RUN_ID) == manifest


# --- failure paths: teardown is guaranteed ---------------------------------------


async def test_agent_timeout_fails_run_but_destroys_sandbox(settings):
    settings.agent_timeout_s = 0.05
    backend = FakeBackend()
    agent = FakeAgent(hang=True)  # never leaves 'running'

    manifest = await execute_run(
        settings, TASK, backend, FakeGateway(), agent, run_id=RUN_ID
    )

    assert manifest.status == "failed"
    assert "dispatch_task" in manifest.error and "timed out" in manifest.error
    assert backend.events == ["provision", "destroy"]  # sandbox still destroyed
    assert manifest.finished_at is not None


async def test_fetch_workspace_failure_fails_run_without_sandbox(settings):
    gateway = FakeGateway()
    gateway.fetch_error = RuntimeError("denied by policy rule: default_deny")
    backend = FakeBackend()

    manifest = await execute_run(settings, TASK, backend, gateway, FakeAgent(), run_id=RUN_ID)

    assert manifest.status == "failed"
    assert manifest.error.startswith("fetch_workspace:")
    assert backend.events == []  # nothing provisioned, nothing to destroy


async def test_registration_failure_still_destroys_provisioned_sandbox(settings):
    gateway = FakeGateway()
    gateway.register_error = RuntimeError("admin endpoint said no")
    backend = FakeBackend()
    agent = FakeAgent()

    manifest = await execute_run(settings, TASK, backend, gateway, agent, run_id=RUN_ID)

    assert manifest.status == "failed"
    assert manifest.error.startswith("provision_sandbox:")
    assert backend.events == ["provision", "destroy"]
    assert agent.dispatched == []  # never reached dispatch_task


async def test_agent_error_result_fails_run(settings):
    agent = FakeAgent(
        terminal={
            "status": "failed",
            "pr_url": None,
            "iterations": 3,
            "error": "tests still failing after 3 iterations",
        }
    )
    backend = FakeBackend()

    manifest = await execute_run(settings, TASK, backend, FakeGateway(), agent, run_id=RUN_ID)

    assert manifest.status == "failed"
    assert manifest.iterations == 3
    assert "still failing" in manifest.error
    assert backend.events == ["provision", "destroy"]
