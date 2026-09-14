"""Crew fulfillment on the live cluster.

The full fulfillment path runs with the real machinery: fulfill wins the
compiled crew.request decision at the cluster's OPA as the conductor,
binds the crew and provisions each agent its own pod (and per-run
ServiceAccount); the implementer dispatch then implements the task through
the gateway and opens a real PR as the *ephemeral* implementer, parentage on
every decision. Then the sweep: identities revoked and rows terminal
after teardown, including worker kill.

Pod IPs are not host-routable on kind, so AgentClient is routed through
`kubectl port-forward` (which bypasses NetworkPolicy by design — it rides
the kubelet, not the pod network).
"""

import asyncio
import contextlib
import secrets
import time
from urllib.parse import urlparse

from common.audit import LIFECYCLE_DESTROY_POD, LIFECYCLE_RECORD_REQUEST, LIFECYCLE_REVOKE
from common.ids import new_run_id
from common.topology import agent_url

from .cluster import (
    COMPANY,
    COMPANY_NS,
    PLATFORM_NS,
    audit_story,
    crew_activities,
    gateway_url,
    kubectl,
    mcp_call,
    port_forward,
    projected_token,
    registry,
    run_kubectl,
    sandbox_image,
    worker_settings,
)
from .conftest import TICKETS_DIR, close_issue, close_pr_url, run_implement


@contextlib.contextmanager
def _pod_forwarded_agent_client():
    """Route AgentClient's pod-IP URLs through kubectl port-forwards to the
    ephemeral pods those IPs belong to — one forward per pod, since every crew
    agent runs on its own pod and a dispatch must reach the agent it
    names. Only a URL naming a company-namespace pod IP is rewritten;
    anything else reaches AgentClient untouched."""
    from orchestrator import clients

    stack = contextlib.ExitStack()
    port_by_ip: dict[str, int] = {}

    def pods_by_ip() -> dict[str, str]:
        listing = kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "pods",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}|{.status.podIP}{'\\n'}{end}",
            timeout=30,
        ).splitlines()
        return {
            ip: name for name, _, ip in (line.partition("|") for line in listing) if ip
        }

    def local_url(url: str) -> str:
        ip = urlparse(url).hostname or ""
        if ip not in port_by_ip:
            pod = pods_by_ip().get(ip)
            if pod is None:
                return url
            port_by_ip[ip] = stack.enter_context(
                port_forward(f"pod/{pod}", 8000, namespace=COMPANY_NS)
            )
        return f"http://127.0.0.1:{port_by_ip[ip]}"

    original_init = clients.AgentClient.__init__

    def patched(self, url: str) -> None:
        original_init(self, local_url(url))

    clients.AgentClient.__init__ = patched
    try:
        with stack:
            yield
    finally:
        clients.AgentClient.__init__ = original_init


def test_crew_delivers_a_pr_under_ephemeral_identity(
    stack: None, env: dict, tmp_path, pr_info
) -> None:
    from orchestrator.clients import AgentClient, GatewayClient
    from orchestrator.ephemeral_pods import (
        EphemeralPodProvisioner,
        pod_name,
        service_account_name,
    )
    from orchestrator.identity import ephemeral_agent_id
    from registry import Runs
    from sandbox.k8s_jobs import K8sJobsBackend

    run_id = new_run_id()
    nonce = secrets.token_hex(3)
    task_text = (TICKETS_DIR / "happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    mcp = gateway_url(env)
    orchestrator_token = projected_token(PLATFORM_NS, "orchestrator-worker")
    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    handle = None

    # 1. The compiled crew.request decision is the cluster's OPA to make,
    #    as the conductor: fulfilment asks it before binding anything.
    with registry() as client, port_forward("svc/opa", 8181) as opa_port:
        crew = crew_activities(
            client,
            worker_settings(env, tmp_path, opa_url=f"http://127.0.0.1:{opa_port}"),
            EphemeralPodProvisioner(COMPANY_NS),
        )
        Runs(client).start(run_id, task_text, "implement", None, COMPANY)
        try:
            # 2. The run's single sandbox, exactly as implement provisions
            #    it (the crew works it through the gateway).
            workspace = asyncio.run(
                _fetch_workspace(mcp, orchestrator_token, run_id, env["TARGET_REPO"])
            )
            handle = backend.provision(
                run_id,
                workspace,
                _sandbox_policy(sandbox_image()),
            )
            asyncio.run(_register(mcp, orchestrator_token, run_id, handle))

            # 3. Fulfill: the decision -> bind -> a pod per agent, returned
            #    by crew role with its identity and address.
            with _pod_forwarded_agent_client():
                fulfilled = asyncio.run(
                    crew.fulfill_crew(
                        {
                            "run_id": run_id,
                            "company": COMPANY,
                            "crew_template": "feature-delivery",
                            "conductor": {
                                "agent_id": "pm-1",
                                "role": "pm",
                                "url": agent_url("pm", COMPANY_NS),
                            },
                            "workflow": "implement",
                        }
                    )
                )
                implementer = fulfilled["agents"]["implementer"]
                assert implementer["agent_id"] == implementer_id
                recorded = [
                    e
                    for e in audit_story(run_id)
                    if e["tool"] == LIFECYCLE_RECORD_REQUEST
                ]
                assert recorded and recorded[-1]["decision"] == "allow", recorded
                assert recorded[-1]["agent_id"] == "pm-1"

                # 4. The implementer dispatch carries the task verbatim. It ends
                #    at a completion claim; the separate deliver dispatch
                #    (issued here in the platform verify gate's stead) opens
                #    the PR.
                async def dispatch(procedure: str, text: str) -> dict:
                    async with AgentClient(implementer["url"]) as agent:
                        await agent.dispatch(run_id, procedure, text)
                        while True:
                            status = await agent.status(run_id)
                            if status["status"] != "running":
                                return status
                            await asyncio.sleep(2)

                claim = asyncio.run(dispatch("implement", task_text))
                assert claim["status"] == "done", f"implementer failed: {claim}"
                assert claim["pr_url"] is None, (
                    "the implement dispatch must end at a claim, not a PR"
                )

                # The platform's own verify is green before delivery.
                async def platform_verify() -> dict:
                    async with GatewayClient(mcp, orchestrator_token) as g:
                        return await g.run_tests(run_id)

                verdict = asyncio.run(platform_verify())
                assert verdict["exit_code"] == 0, f"platform verify red: {verdict}"

                status = asyncio.run(dispatch("deliver", task_text))
            assert status["status"] == "done", f"delivery failed: {status}"
            assert status["pr_url"], "no PR from the ephemeral implementer"

            # The PR is real and on the constrained branch.
            pr = pr_info(status["pr_url"])
            assert pr["head"]["ref"] == f"feature/slugify-{nonce}"

            # Parentage on every gateway decision the crew made.
            story = audit_story(run_id)
            crew_events = [
                e
                for e in story
                if e["source"] == "gateway" and e["lifecycle"] == "ephemeral"
            ]
            assert crew_events, "no ephemeral gateway decisions in the story"
            assert all(e["parent_agent_id"] == "pm-1" for e in crew_events)
            assert any(
                e["agent_id"] == implementer_id
                and e["tool"] == "git.open_pr"
                and e["decision"] == "allow"
                for e in crew_events
            ), "the PR was not opened by the ephemeral implementer"

            # An agent outside its compiled allow-list is denied at the
            # gateway: the implementer holds no issue tools. Its SA exists
            # (fulfilment provisioned its pod), so a fresh projected token
            # is the same credential the pod holds.
            implementer_token = projected_token(
                COMPANY_NS, service_account_name(implementer_id)
            )
            denied = mcp_call(
                env,
                implementer_token,
                "issues.create",
                {
                    "run_id": run_id,
                    "repo": env["TARGET_REPO"],
                    "title": "x",
                    "body": "x",
                    "rationale": "red-team: probing the allowlist",
                },
            )
            assert denied.isError and "default_deny" in denied.content[0].text

            # 5. Teardown semantics: revoke + destroy; the implementer's token
            #    dies with its row, the pod with its labels.
            swept = asyncio.run(crew.revoke_participants(run_id))
            assert implementer_id in swept
            gone = asyncio.run(crew.destroy_ephemeral_pods(run_id))
            assert pod_name(implementer_id) in gone
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(_unregister(mcp, orchestrator_token, run_id))
                backend.destroy(handle)
            asyncio.run(crew.destroy_ephemeral_pods(run_id))
            client.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


def test_worker_kill_leaks_no_crew(stack: None, env: dict) -> None:
    """Identities revoked and rows terminal after teardown
    *including* worker kill — the sweep rides the workflow's compensation,
    which Temporal replays on the restarted worker. The heartbeat's own
    implement run stands its crew up (fulfill_crew), so the kill lands on
    a run with live bound ephemeral agents and their pods."""
    from orchestrator.ephemeral_pods import pod_name
    from orchestrator.identity import ephemeral_agent_id

    record = run_implement("happy", env, kill_after="crew")
    close_pr_url(env, record.get("pr_url"))
    close_issue(env, record["issue_number"])
    run_id = record["run_id"]
    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)

    # Nothing leaked: the row is terminal and the pod is gone.
    with registry() as client:
        rows = client.query(
            "SELECT status FROM registry.run_participants WHERE agent_id = %s",
            (implementer_id,),
        )
        assert rows and rows[0]["status"] == "terminated", rows
        client.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
        )
    # "Survived" means still there, not still there *this instant*. A
    # cancelled fulfillment retry can be mid-`provision` in a worker thread
    # when the workflow returns — `asyncio.to_thread` cannot interrupt it —
    # so a doomed pod may appear for a few seconds and then be collected.
    # The bound keeps this honest: the leak this test exists to catch — a
    # provision landing past teardown with nothing left to collect it —
    # leaves pods Running indefinitely, so a real leak still fails here.
    deadline = time.monotonic() + 90
    while True:
        pods = run_kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "pod",
            pod_name(implementer_id),
            "-o",
            "name",
            timeout=30,
        )
        if pods.returncode != 0:
            break
        assert time.monotonic() < deadline, (
            f"the ephemeral pod survived teardown: {pods.stdout.strip()}"
        )
        time.sleep(3)

    story = audit_story(run_id)
    assert any(e["tool"] == LIFECYCLE_REVOKE for e in story)
    assert any(e["tool"] == LIFECYCLE_DESTROY_POD for e in story)


# -- plumbing ----------------------------------------------------------------


async def _fetch_workspace(gateway_url, token, run_id, repo) -> bytes:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        return await gateway.fetch_workspace(run_id, repo, "main")


async def _register(gateway_url, token, run_id, handle) -> None:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        await gateway.register_sandbox(
            run_id, handle.url, handle.namespace, handle.job_name
        )


async def _unregister(gateway_url, token, run_id) -> None:
    from orchestrator.clients import GatewayClient

    async with GatewayClient(gateway_url, token) as gateway:
        await gateway.unregister_sandbox(run_id)


def _sandbox_policy(image: str):
    from sandbox.contract import SandboxPolicy

    return SandboxPolicy(
        image=image,
        workspace_dir="/workspace",
        disk="1Gi",
        deadline_s=900,
        cpu="500m",
        memory="512Mi",
    )
