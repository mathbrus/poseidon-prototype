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
    sandbox_image,
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


def projected_token(namespace: str, service_account: str) -> str:
    """Workload identity: mint the same kind of
    audience-scoped projected token the kubelet mounts, for the real
    ServiceAccounts — the static bearer tokens are gone."""
    proc = subprocess.run(
        ["kubectl", "-n", namespace, "create", "token", service_account,
         "--audience=poseidon-gateway", "--duration=10m"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return proc.stdout.strip()


def test_sandbox_registration_survives_a_gateway_restart(
    stack: None, env: dict, workspace_tar: bytes, tmp_path: Path
):
    import asyncio

    from orchestrator.activities.crew import CrewActivities
    from orchestrator.clients import GatewayClient
    from orchestrator.config import WorkerSettings
    from registry import AuditStore, RunParticipants, Runs

    from .test_crew_identity import _cluster_secret, _registry

    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    # Registration is the orchestrator's platform admin action; `sandbox.*`
    # belongs to the run's ephemeral executor (the lean roster's only
    # execution identity) — two identities, exactly as
    # in a real run.
    runner_token = projected_token(PLATFORM_NS, "orchestrator-worker")
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    token_file = tmp_path / "unused"
    token_file.write_text("unused")
    handle = backend.provision(
        run_id,
        workspace_tar,
        SandboxPolicy(image=sandbox_image(), cpu="500m", memory="512Mi"),
    )

    async def register() -> None:
        async with GatewayClient(gateway_url, runner_token) as gateway:
            await gateway.register_sandbox(
                run_id, handle.url, handle.namespace, handle.job_name
            )

    async def read_through_gateway(agent_token: str) -> dict:
        async with GatewayClient(gateway_url, agent_token) as gateway:
            assert gateway._session is not None
            result = await gateway._session.call_tool(
                "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )
            return result

    with _registry() as registry:
        settings = WorkerSettings(
            gateway_token_file=str(token_file),
            mcp_gateway_url=gateway_url,
            target_repo=env.get("TARGET_REPO", "github.com/example/repo"),
            platform_jwt_key=_cluster_secret("platform-jwt-key",
                                             "PLATFORM_JWT_KEY"),
        )
        crew = CrewActivities(
            settings, RunParticipants(registry), AuditStore(registry)
        )
        Runs(registry).start(run_id, "registry durability", "implement",
                             None, "hello-saas")
        try:
            asyncio.run(register())
            agent_token = asyncio.run(crew.mint_participant({
                "run_id": run_id, "company": "hello-saas",
                "crew_template": "feature-delivery", "member": "executor",
                "role_template": "backend-engineer",
                "parent_agent_id": "pm-1",
            }))["token"]

            # The gateway that accepted the registration is now gone.
            restart_gateway()

            # A brand-new process serves the run's sandbox calls from the
            # registry rather than from a dict it never had a chance to
            # fill (and the participants row — the executor's binding —
            # survived with it).
            result = asyncio.run(read_through_gateway(agent_token))
            assert not result.isError, result.content
            assert "hello.txt" in result.content[0].text

            # The whole episode is one story, across the restart.
            tools = [e["tool"] for e in audit_story(run_id)]
            assert tools[0] == "/admin/sandboxes"
            assert "sandbox.list_dir" in tools
        finally:
            backend.destroy(handle)
            registry.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s",
                (run_id,))
            registry.execute(
                "DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


def test_run_records_live_in_the_registry(implement_run: dict):
    """The `runs` table replaced runs/<id>/manifest.json: the workflow's
    terminal record is a row, and the workflow id maps back to it."""
    record = run_record(implement_run["run_id"])
    assert record is not None, "no run row for a completed run"
    assert record["status"] == "done"
    assert record["pr_url"] == implement_run["pr_url"]
    assert record["workflow_id"] == f"implement-{implement_run['run_id']}"
    assert record["finished_at"] is not None
