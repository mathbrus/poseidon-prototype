"""Registry durability on the live cluster: a run record and its sandbox
registration survive a gateway restart mid-run.

A gateway that kept sandbox addresses in process memory would silently
orphan every in-flight run's `sandbox.*` calls on restart — the workflow
keeps going and every tool call fails with "no sandbox registered". This
test kills the gateway between provisioning a sandbox and using it.
"""

import asyncio
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.contract import SandboxPolicy
from sandbox.k8s_jobs import K8sJobsBackend

from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    audit_story,
    crew_activities,
    gateway_url,
    kubectl,
    projected_token,
    registry,
    restart_gateway,
    run_kubectl,
    run_record,
    sandbox_image,
    worker_settings,
    workspace_tar,
)


@pytest.fixture
def workspace(tmp_path: Path) -> bytes:
    return workspace_tar(tmp_path)


def test_sandbox_registration_survives_a_gateway_restart(
    stack: None, env: dict, workspace: bytes, tmp_path: Path
):
    from orchestrator.clients import GatewayClient
    from registry import Runs

    # Registration is the orchestrator's platform admin action; `sandbox.*`
    # belongs to the run's ephemeral executor (the lean-standing profile's only
    # execution identity) — two identities, exactly as in a real run.
    runner_token = projected_token(PLATFORM_NS, "orchestrator-worker")
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    handle = backend.provision(
        run_id,
        workspace,
        SandboxPolicy(image=sandbox_image(), cpu="500m", memory="512Mi"),
    )

    async def register() -> None:
        async with GatewayClient(gateway_url(env), runner_token) as gateway:
            await gateway.register_sandbox(
                run_id, handle.url, handle.namespace, handle.job_name
            )

    async def read_through_gateway(agent_token: str) -> dict:
        async with GatewayClient(gateway_url(env), agent_token) as gateway:
            assert gateway._session is not None
            return await gateway._session.call_tool(
                "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )

    with registry() as client:
        crew = crew_activities(client, worker_settings(env, tmp_path))
        Runs(client).start(run_id, "registry durability", "implement", None, "hello-saas")
        sa_name = None
        try:
            asyncio.run(register())
            bound = asyncio.run(
                crew.bind_participant(
                    {
                        "run_id": run_id,
                        "company": "hello-saas",
                        "crew_template": "feature-delivery",
                        "crew_role": "executor",
                        "role_template": "backend-engineer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            sa_name = bound["service_account"]
            kubectl("-n", COMPANY_NS, "create", "serviceaccount", sa_name)
            agent_token = projected_token(COMPANY_NS, sa_name)

            # The gateway that accepted the registration is now gone.
            restart_gateway()

            # A brand-new gateway process serves the run's sandbox calls
            # from the registry (and the participants row — the executor's
            # binding — survived with it).
            result = asyncio.run(read_through_gateway(agent_token))
            assert not result.isError, result.content
            assert "hello.txt" in result.content[0].text

            # The whole episode is one story, across the restart.
            tools = [e["tool"] for e in audit_story(run_id)]
            assert tools[0] == "/admin/sandboxes"
            assert "sandbox.list_dir" in tools
        finally:
            backend.destroy(handle)
            if sa_name:
                run_kubectl("-n", COMPANY_NS, "delete", "serviceaccount", sa_name)
            client.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


def test_run_records_live_in_the_registry(implement_run: dict):
    """The workflow's terminal record is a `runs` row, and the workflow id
    maps back to it."""
    record = run_record(implement_run["run_id"])
    assert record is not None, "no run row for a completed run"
    assert record["status"] == "done"
    assert record["pr_url"] == implement_run["pr_url"]
    assert record["workflow_id"] == f"implement-{implement_run['run_id']}"
    assert record["finished_at"] is not None
