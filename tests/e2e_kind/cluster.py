"""Reaching the kind cluster from the host.

Everything the suite needs from the cluster goes through kubectl: port-
forwards on free local ports, the registry read through the postgres pod's
psql (queries bind their parameters as psql variables — never string
interpolation, even for generated ids) or opened as a real `Registry`
client, projected tokens minted for the cluster's own ServiceAccounts, and
the probes the containment tests run inside pods. Nothing here knows which
test is asking.
"""

import asyncio
import contextlib
import io
import json
import socket
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from common.topology import PLATFORM_NAMESPACE_DEFAULT, RUN_ID_LABEL, TOKEN_AUDIENCE
from registry import Registry, open_registry

PLATFORM_NS = PLATFORM_NAMESPACE_DEFAULT
COMPANY_NS = "hello-saas"
POSTGRES_USER = "poseidon"

# --- kubectl -----------------------------------------------------------------


def run_kubectl(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """`kubectl …`, outcome included — for callers that expect a failure
    (a torn-down namespace, a denied `can-i`)."""
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout
    )


def kubectl(*args: str, timeout: int = 60) -> str:
    """`kubectl …` that must succeed; its stdout, stripped."""
    proc = run_kubectl(*args, timeout=timeout)
    assert proc.returncode == 0, f"kubectl {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def port_forward(
    target: str, remote_port: int, namespace: str = PLATFORM_NS
) -> Iterator[int]:
    """`kubectl port-forward` to `target` (`svc/name` or `pod/name`) on a
    free local port, yielded once it accepts connections. Free ports
    instead of a hand-kept table: two modules can never collide."""
    local = free_port()
    proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", target, f"{local}:{remote_port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail(f"port-forward to {target} exited early")
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail(f"port-forward to {target} never came up")
                time.sleep(0.5)
        yield local
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@contextlib.contextmanager
def temporal_port_forward() -> Iterator[str]:
    """The Temporal frontend as a client address (`host:port`)."""
    with port_forward("svc/temporal-frontend", 7233) as port:
        yield f"127.0.0.1:{port}"


def cluster_secret(name: str, key: str) -> str:
    value = kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "secret",
        name,
        "-o",
        f'go-template={{{{index .data "{key}" | base64decode}}}}',
    )
    assert value, f"secret {name} carries no {key}"
    return value


def deployment_env(deployment: str, name: str, namespace: str = PLATFORM_NS) -> str:
    """One env value off a deployment's pod template — how the suite learns
    the content-addressed images this cluster actually carries. Never
    hardcode `poseidon-sandbox`: the bare name means `:latest`, which no
    kind-up.sh cluster carries."""
    value = kubectl(
        "-n",
        namespace,
        "get",
        f"deployment/{deployment}",
        "-o",
        f'jsonpath={{.spec.template.spec.containers[0].env[?(@.name=="{name}")].value}}',
    )
    assert value, f"{deployment} carries no {name} env"
    return value


def sandbox_image() -> str:
    return deployment_env("orchestrator-worker", "SANDBOX_IMAGE")


def agent_image() -> str:
    return deployment_env("orchestrator-worker", "AGENT_IMAGE")


def projected_token(
    namespace: str, service_account: str, audience: str = TOKEN_AUDIENCE
) -> str:
    """Workload identity, minted through the TokenRequest API: the same
    kind of audience-scoped projected token the kubelet mounts, for one
    of the cluster's real ServiceAccounts."""
    return kubectl(
        "-n",
        namespace,
        "create",
        "token",
        service_account,
        f"--audience={audience}",
        "--duration=10m",
    )


# --- pods --------------------------------------------------------------------


def sandbox_pods(run_id: str | None = None) -> list[str]:
    """Names of live (non-terminated) sandbox pods, optionally for one run.
    Sandboxes are Jobs in the company namespace."""
    selector = f"{RUN_ID_LABEL}={run_id}" if run_id else RUN_ID_LABEL
    listing = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "pods",
        "-l",
        selector,
        "-o",
        "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{'\\n'}{end}",
        timeout=30,
    )
    return [
        name
        for line in listing.splitlines()
        if line.strip()
        for name, _, phase in [line.partition(" ")]
        if phase.strip() not in ("Succeeded", "Failed")
    ]


def kill_worker_pod() -> None:
    kubectl(
        "-n",
        PLATFORM_NS,
        "delete",
        "pod",
        "-l",
        "app=orchestrator-worker",
        "--wait=false",
    )


def restart_gateway() -> None:
    """Delete the gateway pod and wait for its replacement to be Ready —
    the durability contract: a run's sandbox registration must survive it."""
    kubectl(
        "-n",
        PLATFORM_NS,
        "delete",
        "pod",
        "-l",
        "app=mcp-gateway",
        "--wait=true",
        timeout=120,
    )
    kubectl(
        "-n",
        PLATFORM_NS,
        "rollout",
        "status",
        "deployment/mcp-gateway",
        "--timeout=180s",
        timeout=200,
    )


def agent_pod(namespace: str = COMPANY_NS) -> str:
    """The standing agent's pod — the vantage point for containment probes."""
    return kubectl(
        "-n",
        namespace,
        "get",
        "pod",
        "-l",
        "poseidon.dev/kind=standing-agent",
        "-o",
        "jsonpath={.items[0].metadata.name}",
        timeout=30,
    )


# Any HTTP answer (401 included) counts as reachable; "blocked" must fail at
# the transport layer within the timeout.
PROBE = """
import sys, urllib.request, urllib.error
url, want = sys.argv[1], sys.argv[2]
try:
    urllib.request.urlopen(url, timeout=3)
    reachable = True
except urllib.error.HTTPError:
    reachable = True
except Exception:
    reachable = False
sys.exit(0 if reachable == (want == "reachable") else 1)
"""


def probe(pod: str, url: str, want: str, namespace: str = COMPANY_NS) -> bool:
    """Whether `url` is `reachable` / `blocked` from inside `pod`."""
    proc = run_kubectl(
        "-n", namespace, "exec", pod, "--", "python", "-c", PROBE, url, want
    )
    return proc.returncode == 0


# --- the registry ------------------------------------------------------------


def psql(sql: str, **params: Any) -> str:
    """Run `sql` through the postgres pod's psql and return its output,
    stripped. Every keyword argument is bound as a psql variable — write
    `:'name'` in the query text — so no value, generated or not, is ever
    interpolated into SQL."""
    bindings = [
        arg for name, value in params.items() for arg in ("-v", f"{name}={value}")
    ]
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "exec",
            "-i",
            "statefulset/postgres",
            "--",
            "psql",
            "-U",
            POSTGRES_USER,
            "-d",
            POSTGRES_USER,
            "-At",
            *bindings,
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}"
    return proc.stdout.strip()


def psql_json(sql: str, default: str = "[]", **params: Any) -> Any:
    return json.loads(psql(sql, **params) or default)


def db_now() -> str:
    """The registry database's own clock — the fence for "what this suite
    execution really produced", immune to host clock skew."""
    stamp = psql("SELECT now()")
    assert stamp
    return stamp


@contextlib.contextmanager
def registry() -> Iterator[Registry]:
    """The cluster's registry over a port-forward — the same accessors the
    in-cluster worker uses, pointed at the same database."""
    with port_forward("svc/postgres", 5432) as port:
        password = cluster_secret("postgres-credentials", "POSTGRES_PASSWORD")
        client = open_registry(
            f"postgresql://{POSTGRES_USER}:{password}@127.0.0.1:{port}/{POSTGRES_USER}",
            migrate=False,
        )
        try:
            yield client
        finally:
            client.close()


def audit_story(run_id: str) -> list[dict[str, Any]]:
    """The whole story for one run, from the registry."""
    return psql_json(
        "SELECT COALESCE(json_agg(e ORDER BY e.ts, e.id), '[]'::json) FROM ("
        "SELECT * FROM registry.audit_events WHERE run_id = :'run_id'"
        ") e",
        run_id=run_id,
    )


def events_since(fence: str) -> list[dict[str, Any]]:
    """Every audit event stamped at or after `fence` (a `db_now()` value)."""
    return psql_json(
        "SELECT COALESCE(json_agg(e), '[]'::json) FROM ("
        "SELECT source, run_id, decision, rule, tool"
        " FROM registry.audit_events WHERE ts >= :'fence') e",
        fence=fence,
    )


def authn_denies(rule: str, principal: str) -> list[dict[str, Any]]:
    """Recent authn deny events for one rule and principal. They carry no
    run_id — the credential never mapped to one."""
    return psql_json(
        "SELECT COALESCE(json_agg(e ORDER BY e.ts), '[]'::json) FROM ("
        "SELECT * FROM registry.audit_events"
        " WHERE decision = 'deny' AND rule = :'rule' AND agent_id = :'principal'"
        "   AND ts > now() - interval '5 minutes'"
        ") e",
        rule=rule,
        principal=principal,
    )


def run_record(run_id: str) -> dict[str, Any] | None:
    """The registry's own view of a run — what survives a worker restart."""
    return psql_json(
        "SELECT COALESCE(row_to_json(r), 'null'::json) FROM registry.runs r "
        "WHERE run_id = :'run_id'",
        default="null",
        run_id=run_id,
    )


def run_by_task(task: str, workflow: str | None = None) -> dict[str, Any] | None:
    """A run by its canonical task text — how a heartbeat-started run,
    whose id nobody on the host chose, is found."""
    return psql_json(
        "SELECT COALESCE(row_to_json(r), 'null'::json) FROM registry.runs r "
        "WHERE r.task = :'task' AND (:'workflow' = '' OR r.workflow = :'workflow') "
        "LIMIT 1",
        default="null",
        task=task,
        workflow=workflow or "",
    )


def runs_by_task(task: str, workflow: str | None = None) -> list[dict[str, Any]]:
    """Every run on one canonical task, oldest first — the attempts the
    heartbeat made on one issue or PR."""
    return psql_json(
        "SELECT COALESCE(json_agg(r ORDER BY r.created_at), '[]'::json) "
        "FROM registry.runs r "
        "WHERE r.task = :'task' AND (:'workflow' = '' OR r.workflow = :'workflow')",
        task=task,
        workflow=workflow or "",
    )


def await_run(task: str, terminal: bool, deadline_s: int = 1800) -> dict[str, Any]:
    """Poll the registry for a run by its task — optionally until it
    reaches a terminal state."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        run = run_by_task(task)
        if run and (not terminal or run["status"] in ("done", "failed")):
            return run
        time.sleep(5)
    raise AssertionError(f"run {task!r} never {'finished' if terminal else 'started'}")


def run_spend(run_id: str) -> float:
    """The run's ledger spend — the per-run hard stop is enforced there,
    not in the audit stream (audit events carry no usd)."""
    return float(
        psql(
            "SELECT COALESCE(SUM(usd), 0) FROM registry.budget_ledger"
            " WHERE run_id = :'run_id'",
            run_id=run_id,
        )
        or 0
    )


def participants(run_id: str) -> list[dict[str, Any]]:
    """The run's crew, as the registry bound it."""
    return psql_json(
        "SELECT COALESCE(json_agg(row_to_json(p)), '[]'::json)"
        " FROM registry.run_participants p WHERE p.run_id = :'run_id'",
        run_id=run_id,
    )


def non_terminal_runs() -> list[dict[str, Any]]:
    return psql_json(
        "SELECT COALESCE(json_agg(json_build_object("
        "'run_id', r.run_id, 'workflow', r.workflow, 'status', r.status)), '[]'::json)"
        " FROM registry.runs r WHERE r.status NOT IN ('done', 'failed')"
    )


def running_workflows() -> list[str]:
    """Every Temporal workflow still running, from the admin tools pod."""
    listing = kubectl(
        "-n",
        PLATFORM_NS,
        "exec",
        "-i",
        "deploy/temporal-admintools",
        "--",
        "temporal",
        "workflow",
        "list",
        "--address",
        "temporal-frontend:7233",
        "--query",
        "ExecutionStatus='Running'",
    )
    return [
        line.strip()
        for line in listing.splitlines()
        if line.strip() and not line.strip().startswith("Status")
    ]


def await_egress_deny(run_id: str, timeout_s: int = 30) -> list[dict[str, Any]]:
    """The run's audited egress denies (`source=sandbox-infra`,
    `egress_not_allowlisted`), polled until at least one lands: the proxy
    writes the audit row off the request path."""
    deadline = time.monotonic() + timeout_s
    while True:
        denies = [
            e
            for e in audit_story(run_id)
            if e["source"] == "sandbox-infra"
            and e["decision"] == "deny"
            and e["rule"] == "egress_not_allowlisted"
        ]
        if denies or time.monotonic() > deadline:
            return denies
        time.sleep(1)


# --- workspaces ---------------------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


def git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def workspace_tar(
    path: Path, files: dict[str, str] | None = None, remote: str | None = None
) -> bytes:
    """A committed git workspace as the tarball `provision` delivers: one
    `main` commit of `files` (a hello file by default), optionally with a
    remote configured — the shape the sandbox is expected to strip."""
    for name, content in (files or {"hello.txt": "hello\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(path, "init", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-m", "base")
    if remote:
        git(path, "remote", "add", "origin", remote)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in path.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


# --- acting as the platform or an agent ----------------------------------------


def gateway_url(env: dict) -> str:
    return env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")


def mcp_call(env: dict, token: str, tool: str, args: dict[str, Any]):
    """One tool call through the gateway's MCP surface as whoever `token`
    is — the raw result, `isError` and all, so a policy denial reads as
    the denial it was."""
    from orchestrator.clients import GatewayClient

    async def call():
        async with GatewayClient(gateway_url(env), token) as gateway:
            return await gateway._session.call_tool(tool, args)

    return asyncio.run(call())


def admin_sandbox(env: dict, action: str, run_id: str, handle=None) -> None:
    """Register (`action="register"`, with the backend handle) or retire a
    run's sandbox as the orchestrator would: the registration is both the
    gateway's run binding and the egress proxy's path to the blueprint's
    allowlist."""
    from orchestrator.clients import GatewayClient

    token = projected_token(PLATFORM_NS, "orchestrator-worker")

    async def run() -> None:
        async with GatewayClient(gateway_url(env), token) as gateway:
            if action == "register":
                await gateway.register_sandbox(
                    run_id, handle.url, handle.namespace, handle.job_name
                )
            else:
                await gateway.unregister_sandbox(run_id)

    asyncio.run(run())


def egress_proxy_url() -> str:
    """The proxy address as a sandbox must receive it: an address, not a
    name. The orchestrator resolves the Service name in-cluster at
    provision time (the sandbox has no resolver); on the host that name
    does not resolve, so ask Kubernetes for the same ClusterIP."""
    ip = kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "svc",
        "egress-proxy",
        "-o",
        "jsonpath={.spec.clusterIP}",
    )
    assert ip, "the egress-proxy Service has no ClusterIP"
    return f"http://{ip}:3128"


def egress_proxy_key() -> str:
    return cluster_secret("egress-proxy-key", "EGRESS_PROXY_KEY")


def worker_settings(env: dict, tmp_path: Path):
    """A `WorkerSettings` for running the orchestrator's activities from
    the host against this cluster: the gateway token file is a placeholder
    (host-side tests call the gateway with tokens they mint themselves),
    the images are the ones the cluster carries."""
    from orchestrator.config import WorkerSettings

    token_file = tmp_path / "unused-token"
    token_file.write_text("unused")
    return WorkerSettings(
        temporal_address="127.0.0.1:7233",
        company=COMPANY_NS,
        gateway_token_file=str(token_file),
        mcp_gateway_url=gateway_url(env),
        agent_image=agent_image(),
        model="mock-brain",
    )


def crew_activities(client: Registry, settings, provisioner=None):
    """The real `CrewActivities` over the cluster's registry — bind,
    revoke, provision and destroy exactly as the in-cluster worker does."""
    from orchestrator.activities.crew import CrewActivities
    from registry import (
        AuditStore,
        BudgetLedger,
        Companies,
        CrewRequests,
        RunParticipants,
        Runs,
    )

    return CrewActivities(
        settings,
        RunParticipants(client),
        AuditStore(client),
        CrewRequests(client),
        Companies(client),
        BudgetLedger(client),
        Runs(client),
        provisioner=provisioner,
    )
