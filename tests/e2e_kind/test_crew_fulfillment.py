"""Step 16d done-when — the §4.4 exit criteria on the live cluster.

The full fulfillment path runs with the real machinery: the PM records
intent through the gateway (policy-faced), fulfill consumes it, mints the
crew, provisions the executor's brain-host pod, dispatches the planner on
it under the planner's own identity, and hands the brief to the executor
dispatch — which implements the task through the gateway and opens a real
PR as the *ephemeral* executor, parentage on every decision. Then the
sweep: identities revoked and rows terminal after teardown, including
worker kill.

Pod IPs are not host-routable on kind, so AgentClient is shimmed through
`kubectl port-forward` (which bypasses NetworkPolicy by design — it rides
the kubelet, not the pod network).
"""

import asyncio
import contextlib
import secrets
import socket
import subprocess
import time

import pytest

from common.ids import new_run_id
from orchestrator.workflows.implement import (
    FAST_TIMEOUTS,
    ImplementInput,
    workflow_id,
)

from .conftest import (
    COMPANY_NS,
    PLATFORM_NS,
    REPO_ROOT,
    TASK_QUEUE,
    audit_story,
    sandbox_image,
    sandbox_pods,
    temporal_port_forward,
)
from .test_crew_identity import _cluster_secret, _registry
from .test_governance import _call
from .test_registry_durability import projected_token

AGENT_LOCAL_PORT = 18001


def _worker_settings(env: dict, tmp_path, registry_unused=None):
    from orchestrator.config import WorkerSettings

    token_file = tmp_path / "unused"
    token_file.write_text("unused")
    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "get", "deployment/orchestrator-worker",
         "-o", 'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="AGENT_IMAGE")].value}'],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url=env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080"),
        agent_url="http://unused.test:8000",
        target_repo=env["TARGET_REPO"],
        platform_jwt_key=_cluster_secret("platform-jwt-key", "PLATFORM_JWT_KEY"),
        agent_image=proc.stdout.strip(),
        model="mock-brain",
    )


@contextlib.contextmanager
def _pod_forwarded_agent_client(pod: str):
    """Route AgentClient's pod-IP URLs through a kubectl port-forward to
    the (deterministically named) crew pod."""
    import httpx

    from orchestrator import clients

    proc: subprocess.Popen | None = None

    def ensure_forward() -> None:
        nonlocal proc
        if proc is not None:
            return
        proc = subprocess.Popen(
            ["kubectl", "-n", COMPANY_NS, "port-forward", f"pod/{pod}",
             f"{AGENT_LOCAL_PORT}:8000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", AGENT_LOCAL_PORT),
                                              timeout=1):
                    return
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("port-forward to the crew pod never came up")
                time.sleep(0.5)

    original_init = clients.AgentClient.__init__

    def patched(self, url: str) -> None:
        if "svc.cluster.local" not in url and "unused.test" not in url:
            ensure_forward()
            url = f"http://127.0.0.1:{AGENT_LOCAL_PORT}"
        original_init(self, url)

    clients.AgentClient.__init__ = patched
    try:
        yield
    finally:
        clients.AgentClient.__init__ = original_init
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)


def test_crew_delivers_a_pr_under_ephemeral_identity(
    stack: None, env: dict, tmp_path, pr_info
) -> None:
    from orchestrator.activities.crew import CrewActivities
    from orchestrator.clients import AgentClient, GatewayClient
    from orchestrator.crewpods import CrewPodProvisioner, pod_name
    from orchestrator.identity import ephemeral_agent_id
    from registry import AuditStore, CrewRequests, RunParticipants, Runs
    from registry.bundles import Companies
    from sandbox.backends.k8s_jobs import K8sJobsBackend

    run_id = new_run_id()
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    orchestrator_token = projected_token(PLATFORM_NS, "orchestrator-worker")
    executor_id = ephemeral_agent_id("feature-delivery", "executor", run_id)
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    handle = None

    with _registry() as registry:
        settings = _worker_settings(env, tmp_path)
        crew = CrewActivities(
            settings,
            RunParticipants(registry),
            AuditStore(registry),
            CrewPodProvisioner(COMPANY_NS),
            CrewRequests(registry),
            Companies(registry),
        )
        Runs(registry).start(run_id, task_text, "implement", None, "hello-saas")
        try:
            # 1. The PM records intent through the gateway — same row, same
            #    policy as any spawn (§4.4 rule 3); may_spawn/cap/budget
            #    checks all faced here.
            pm_token = projected_token(COMPANY_NS, "pm-1")
            recorded = _call(env, pm_token, "crew.request",
                             {"run_id": run_id,
                              "crew_template": "feature-delivery",
                              "reason": "e2e crew delivery"})
            assert not recorded.isError, recorded.content

            # 2. The run's single sandbox, exactly as implement provisions
            #    it (both members share it through the gateway).
            workspace = asyncio.run(_fetch_workspace(
                gateway_url, orchestrator_token, run_id, env["TARGET_REPO"]))
            handle = backend.provision(
                run_id, workspace,
                _sandbox_policy(sandbox_image()),
            )
            asyncio.run(_register(gateway_url, orchestrator_token, run_id, handle))

            # 3. Fulfill: consume -> mint -> pod -> planner -> brief.
            with _pod_forwarded_agent_client(pod_name(executor_id)):
                fulfilled = asyncio.run(crew.fulfill_crew({
                    "run_id": run_id, "company": "hello-saas",
                    "crew_template": "feature-delivery", "conductor": "pm-1",
                    "task_text": task_text,
                }))
                assert f"feature/slugify-{nonce}" in fulfilled["brief"], (
                    "the brief lost the delivery constraint"
                )

                # 4. The executor dispatch carries the brief verbatim — the
                #    orchestrator-fixed graph IS the handoff (§4.4 rule 6).
                async def run_executor() -> dict:
                    async with AgentClient(fulfilled["executor_url"]) as agent:
                        await agent.dispatch(run_id, fulfilled["brief"])
                        while True:
                            status = await agent.status(run_id)
                            if status["status"] != "running":
                                return status
                            await asyncio.sleep(2)

                status = asyncio.run(run_executor())
            assert status["status"] == "done", f"executor failed: {status}"
            assert status["pr_url"], "no PR from the ephemeral executor"

            # The PR is real and on the constrained branch.
            pr = pr_info(status["pr_url"])
            assert pr["head"]["ref"] == f"feature/slugify-{nonce}"

            # §4.4 exit: parentage on every gateway decision the crew made.
            story = audit_story(run_id)
            crew_events = [e for e in story if e["source"] == "gateway"
                           and e["lifecycle"] == "ephemeral"]
            assert crew_events, "no ephemeral gateway decisions in the story"
            assert all(e["parent_agent_id"] == "pm-1" for e in crew_events)
            assert any(e["agent_id"] == executor_id
                       and e["tool"] == "git.open_pr"
                       and e["decision"] == "allow" for e in crew_events), (
                "the PR was not opened by the ephemeral executor"
            )
            # The planner acted too, as itself.
            planner_id = ephemeral_agent_id("feature-delivery", "planner", run_id)
            assert any(e["agent_id"] == planner_id for e in crew_events)

            # A member outside its compiled allow-list is denied at the
            # gateway: the read-only planner cannot write.
            planner_token = asyncio.run(crew.mint_participant({
                "run_id": run_id, "company": "hello-saas",
                "crew_template": "feature-delivery", "member": "planner",
                "role_template": "backend-engineer", "parent_agent_id": "pm-1",
            }))["token"]
            denied = _call(env, planner_token, "sandbox.write_file",
                           {"run_id": run_id, "path": "x.py", "content": "x"})
            assert denied.isError and "default_deny" in denied.content[0].text

            # 5. Teardown semantics: revoke + destroy; the executor's token
            #    dies with its row, the pod with its labels.
            swept = asyncio.run(crew.revoke_participants(run_id))
            assert executor_id in swept
            gone = asyncio.run(crew.destroy_crew_pods(run_id))
            assert pod_name(executor_id) in gone
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(_unregister(gateway_url, orchestrator_token, run_id))
                backend.destroy(handle)
            asyncio.run(crew.destroy_crew_pods(run_id))
            registry.execute(
                "DELETE FROM registry.crew_requests WHERE run_id = %s", (run_id,))
            registry.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,))
            registry.execute(
                "DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


def test_worker_kill_leaks_no_crew(stack: None, env: dict, tmp_path) -> None:
    """§4.4 exit: identities revoked and rows terminal after teardown
    *including* worker kill — the sweep rides the workflow's compensation,
    which Temporal replays on the restarted worker."""
    from orchestrator.activities.crew import CrewActivities
    from orchestrator.crewpods import CrewPodProvisioner, pod_name
    from orchestrator.identity import ephemeral_agent_id
    from registry import AuditStore, CrewRequests, RunParticipants
    from registry.bundles import Companies
    from temporalio.client import Client

    run_id = new_run_id()
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    executor_id = ephemeral_agent_id("feature-delivery", "executor", run_id)

    async def run() -> dict:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            wf = await client.start_workflow(
                "implement",
                ImplementInput(run_id=run_id, task_text=task_text,
                               timeouts=FAST_TIMEOUTS),
                id=workflow_id(run_id),
                task_queue=TASK_QUEUE,
            )
            # Once the run exists mid-flight, attach a crew to it —
            # exactly what a fulfillment would have left behind.
            deadline = time.monotonic() + 300
            while not sandbox_pods(run_id):
                if time.monotonic() > deadline:
                    raise AssertionError("sandbox never appeared")
                await asyncio.sleep(2)
            with _registry() as registry:
                settings = _worker_settings(env, tmp_path)
                crew = CrewActivities(
                    settings, RunParticipants(registry), AuditStore(registry),
                    CrewPodProvisioner(COMPANY_NS), CrewRequests(registry),
                    Companies(registry),
                )
                await crew.mint_participant({
                    "run_id": run_id, "company": "hello-saas",
                    "crew_template": "feature-delivery", "member": "executor",
                    "role_template": "backend-engineer",
                    "parent_agent_id": "pm-1",
                })
                await crew.provision_crew_pod({
                    "run_id": run_id, "company": "hello-saas",
                    "crew_template": "feature-delivery", "member": "executor",
                    "role_template": "backend-engineer",
                    "parent_agent_id": "pm-1",
                })
            # Kill the worker mid-run; the replay finishes the run and its
            # teardown on the replacement pod.
            subprocess.run(
                ["kubectl", "-n", PLATFORM_NS, "delete", "pod",
                 "-l", "app=orchestrator-worker", "--wait=false"],
                check=True, capture_output=True, timeout=60,
            )
            return await asyncio.wait_for(wf.result(), timeout=900)

    record = asyncio.run(run())
    assert record["run_id"] == run_id

    # Nothing leaked: the row is terminal and the pod is gone.
    with _registry() as registry:
        rows = registry.query(
            "SELECT status FROM registry.run_participants WHERE agent_id = %s",
            (executor_id,),
        )
        assert rows and rows[0]["status"] == "terminated", rows
        registry.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,))
    pods = subprocess.run(
        ["kubectl", "-n", COMPANY_NS, "get", "pod", pod_name(executor_id),
         "-o", "name"],
        capture_output=True, text=True, timeout=30,
    )
    assert pods.returncode != 0, "the crew pod survived teardown"

    story = audit_story(run_id)
    assert any(e["tool"] == "lifecycle.revoke" for e in story)
    assert any(e["tool"] == "lifecycle.destroy_pod" for e in story)


# -- plumbing ----------------------------------------------------------------


async def _fetch_workspace(gateway_url, token, run_id, repo) -> bytes:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        return await gateway.fetch_workspace(run_id, repo, "main")


async def _register(gateway_url, token, run_id, handle) -> None:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        await gateway.register_sandbox(run_id, handle.url, handle.namespace,
                                       handle.job_name)


async def _unregister(gateway_url, token, run_id) -> None:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        await gateway.unregister_sandbox(run_id)


def _sandbox_policy(image: str):
    from sandbox.contract import SandboxPolicy

    return SandboxPolicy(image=image, cpu="500m", memory="512Mi")
