"""Step-8 done-when on the live cluster: a run record and its sandbox
registration survive a gateway restart mid-run.

Before the registry, the gateway held sandbox addresses in a process-local
dict. That was fine while a run was a host-side script, but once runs became
durable a gateway restart silently orphaned every in-flight run's
`sandbox.*` calls — the workflow kept going and every tool call failed with
"no sandbox registered". This test kills the gateway between provisioning a
sandbox and using it.
"""

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.backends.k8s_jobs import K8sJobsBackend
from sandbox.contract import SandboxPolicy

from .conftest import (
    COMPANY_NS,
    PLATFORM_NS,
    audit_story,
    restart_gateway,
    run_record,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


@pytest.fixture
def workspace_tar(tmp_path: Path) -> bytes:
    for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "base"]):
        if args[0] == "add":
            (tmp_path / "hello.txt").write_text("hello\n")
        subprocess.run(
            ["git", *args], cwd=tmp_path, env=GIT_ENV, check=True, capture_output=True
        )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in tmp_path.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


def bearer_token(key: str) -> str:
    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "get", "secret", "bearer-tokens",
         "-o", "go-template={{index .data \"" + key + "\" | base64decode}}"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return proc.stdout


def test_sandbox_registration_survives_a_gateway_restart(
    stack: None, env: dict, workspace_tar: bytes
):
    import asyncio

    from orchestrator.clients import GatewayClient

    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    # Registration is the orchestrator's (runner) admin action; `sandbox.*`
    # belongs to the engineer for its assigned run — two identities, exactly
    # as in a real run.
    runner_token = bearer_token("RUNNER_BEARER_TOKEN")
    agent_token = bearer_token("AGENT_BEARER_TOKEN")
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    handle = backend.provision(
        run_id,
        workspace_tar,
        SandboxPolicy(image="poseidon-sandbox", cpu="500m", memory="512Mi"),
    )

    async def register() -> None:
        async with GatewayClient(gateway_url, runner_token) as gateway:
            await gateway.register_sandbox(
                run_id, handle.url, handle.namespace, handle.job_name
            )

    async def read_through_gateway() -> dict:
        async with GatewayClient(gateway_url, agent_token) as gateway:
            assert gateway._session is not None
            result = await gateway._session.call_tool(
                "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )
            return result

    try:
        asyncio.run(register())

        # The gateway that accepted the registration is now gone.
        restart_gateway()

        # A brand-new process serves the run's sandbox calls from the
        # registry rather than from a dict it never had a chance to fill.
        result = asyncio.run(read_through_gateway())
        assert not result.isError, result.content
        assert "hello.txt" in result.content[0].text

        # The whole episode is one story, across the restart.
        tools = [e["tool"] for e in audit_story(run_id)]
        assert tools[0] == "/admin/sandboxes"
        assert "sandbox.list_dir" in tools
    finally:
        backend.destroy(handle)


def test_run_records_live_in_the_registry(implement_run: dict):
    """The `runs` table replaced runs/<id>/manifest.json: the workflow's
    terminal record is a row, and the workflow id maps back to it (§5.5)."""
    record = run_record(implement_run["run_id"])
    assert record is not None, "no run row for a completed run"
    assert record["status"] == "done"
    assert record["pr_url"] == implement_run["pr_url"]
    assert record["workflow_id"] == f"implement-{implement_run['run_id']}"
    assert record["finished_at"] is not None
