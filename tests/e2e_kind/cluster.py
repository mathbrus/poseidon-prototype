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
import os
import socket
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.topology import (
    COMPANY_NAMESPACE,
    CONSOLE_TOKEN_AUDIENCE,
    RUN_ID_LABEL,
    TOKEN_AUDIENCE,
    human_service_account,
)
from common.workflows import workflow_id
from poseidon_cli import ConsoleClient
from registry import Registry, open_registry

PLATFORM_NS = "poseidon-platform"  # the chart release namespace kind-up installs into
# The suite's company is the shipped example. Its name keys everything the
# platform records (registry rows, workflow ids, audit events, the Rego
# package); its namespace is the platform's fixed one and names none of it.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
HELLO_SAAS = EXAMPLES / "hello-saas.yaml"
DELEGATED = EXAMPLES / "hello-saas-delegated.yaml"
COMPANY: str = yaml.safe_load(HELLO_SAAS.read_text())["company"]["name"]
COMPANY_NS = COMPANY_NAMESPACE
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
    namespace: str,
    service_account: str,
    audience: str = TOKEN_AUDIENCE,
    duration: str = "10m",
) -> str:
    """An identity minted through the TokenRequest API: the same kind of
    audience-scoped token the kubelet mounts for a workload, or a person
    mints for themselves, for one of the cluster's real ServiceAccounts."""
    return kubectl(
        "-n",
        namespace,
        "create",
        "token",
        service_account,
        f"--audience={audience}",
        f"--duration={duration}",
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


def platform_pod(app: str) -> str:
    """A running platform pod by its `app` label — the vantage point for a
    probe of what the platform namespace's own pods may reach."""
    return kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "pod",
        "-l",
        f"app={app}",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[0].metadata.name}",
        timeout=30,
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


# A port that speaks no HTTP (Temporal's gRPC frontend) is probed for a TCP
# connection alone: accepted is reachable, refused or dropped is blocked.
TCP_PROBE = """
import socket, sys
host, port, want = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    socket.create_connection((host, port), timeout=3).close()
    reachable = True
except Exception:
    reachable = False
sys.exit(0 if reachable == (want == "reachable") else 1)
"""


def probe_tcp(pod: str, host: str, port: int, want: str, namespace: str) -> bool:
    """Whether `host:port` accepts a connection from inside `pod`."""
    proc = run_kubectl(
        "-n",
        namespace,
        "exec",
        pod,
        "--",
        "python",
        "-c",
        TCP_PROBE,
        host,
        str(port),
        want,
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


def authn_denies(
    rule: str, principal: str, source: str, since: str | None = None
) -> list[dict[str, Any]]:
    """Authn deny events for one rule and principal from one door, since a
    registry timestamp (the last five minutes when none). They carry no
    run_id — the credential never mapped to one."""
    return psql_json(
        "SELECT COALESCE(json_agg(e ORDER BY e.ts), '[]'::json) FROM ("
        "SELECT * FROM registry.audit_events"
        " WHERE decision = 'deny' AND rule = :'rule' AND agent_id = :'principal'"
        "   AND source = :'source'"
        "   AND ts > COALESCE(NULLIF(:'since', '')::timestamptz,"
        "                     now() - interval '5 minutes')"
        ") e",
        rule=rule,
        principal=principal,
        source=source,
        since=since or "",
    )


def run_record(run_id: str) -> dict[str, Any] | None:
    """The registry's own view of a run — what survives a worker restart."""
    return psql_json(
        "SELECT COALESCE(row_to_json(r), 'null'::json) FROM registry.runs r "
        "WHERE run_id = :'run_id'",
        default="null",
        run_id=run_id,
    )


def run_by_task(
    task: str, workflow: str | None = None, since: str | None = None
) -> dict[str, Any] | None:
    """The latest run on one canonical task text — how a heartbeat-started
    run, whose id nobody on the host chose, is found. Retries share a
    task, and every scope run works the same one, so the latest is the run
    a module means; `since` (a registry timestamp from `db_now`) narrows
    to runs born after the module's own trigger."""
    return psql_json(
        "SELECT COALESCE(row_to_json(r), 'null'::json) FROM registry.runs r "
        "WHERE r.task = :'task' AND (:'workflow' = '' OR r.workflow = :'workflow') "
        "AND r.created_at > COALESCE(NULLIF(:'since', ''), '-infinity')::timestamptz "
        "ORDER BY r.created_at DESC LIMIT 1",
        default="null",
        task=task,
        workflow=workflow or "",
        since=since or "",
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


def await_run(
    task: str,
    terminal: bool,
    deadline_s: int = 1800,
    workflow: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Poll the registry for the latest run on `task` (of `workflow`, born
    after `since`, when given) — optionally until it reaches a terminal
    state."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        run = run_by_task(task, workflow=workflow, since=since)
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


# --- acting as a human: the console --------------------------------------------
#
# Every act the suite performs on the company — apply a profile, wake the
# loop, answer a gate, start a run by hand — goes through the console as
# the human the chart declares (`owner`), exactly as a person does. What the
# suite *observes* it reads with its own instruments (psql, kubectl, the
# git host, a raw Temporal port-forward for the red-team), never the
# product's word for it.

HUMAN = "owner"
# A client per human, renewed with its token: the token lives ten
# minutes, the client eight, so no call ever rides an expired one and a
# module's polling is one connection, not a socket per poll.
_CLIENT_TTL_S = 8 * 60
_clients: dict[str, tuple[ConsoleClient, float]] = {}


def console_url(env: dict | None = None) -> str:
    """Where the console is, as `.env` says — else the client's own default."""
    return (env or {}).get("POSEIDON_CONSOLE_URL") or ConsoleClient().base_url


def human_token(name: str = HUMAN, duration: str = "10m") -> str:
    """A token for the console's audience, minted for one of the humans
    the chart declares — the same TokenRequest `poseidon login` makes."""
    return projected_token(
        PLATFORM_NS, human_service_account(name), CONSOLE_TOKEN_AUDIENCE, duration
    )


def console(env: dict | None = None, name: str = HUMAN) -> ConsoleClient:
    """The console, as the human `name` — the one way the suite acts."""
    cached = _clients.get(name)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    if cached:
        cached[0].close()
    client = ConsoleClient(console_url(env), human_token(name))
    _clients[name] = (client, time.monotonic() + _CLIENT_TTL_S)
    return client


def console_env(env: dict | None = None) -> dict[str, str]:
    """The environment the real CLI needs to act as `owner` — a fresh
    token that outlives the longest command (`poseidon run` waits for a
    whole implement run): for the modules that exercise `poseidon …`
    itself."""
    return {
        **os.environ,
        "POSEIDON_CONSOLE_URL": console_url(env),
        "POSEIDON_TOKEN": human_token(duration="1h"),
    }


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


def worker_settings(env: dict, tmp_path: Path, opa_url: str = "http://opa.test:8181"):
    """A `WorkerSettings` for running the orchestrator's activities from
    the host against this cluster: the gateway token file is a placeholder
    (host-side tests call the gateway with tokens they mint themselves),
    the images are the ones the cluster carries. `opa_url` names a
    port-forward to the cluster's OPA when the activity under test wins a
    compiled decision (crew fulfilment)."""
    from orchestrator.config import WorkerSettings

    token_file = tmp_path / "unused-token"
    token_file.write_text("unused")
    return WorkerSettings(
        opa_url=opa_url,
        platform_namespace="poseidon-platform",
        sandbox_image="poseidon-sandbox:abc123",
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        temporal_address="127.0.0.1:7233",
        company=COMPANY,
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
        RunParticipants,
        Runs,
    )

    return CrewActivities(
        settings,
        RunParticipants(client),
        AuditStore(client),
        Companies(client),
        BudgetLedger(client),
        Runs(client),
        provisioner=provisioner,
    )


# --- the standing heartbeat: its id, its ticks, its wake and its sleep -----


def heartbeat_ticks(heartbeat_id: str) -> list[dict[str, Any]]:
    """The tick rows one heartbeat workflow wrote — the ledger journals
    every tick under the shared `heartbeat-{company}` run id, and each row
    carries the writing workflow's own id in the `workflow` column. The
    standing heartbeat keeps its id across restarts, so this list grows
    for as long as the session runs: scope assertions to a window (count
    the rows, act, assert on the rows past the mark) or to rules that
    name a run, never to the whole history."""
    return [
        e
        for e in audit_story(workflow_id("heartbeat", COMPANY))
        if e["tool"] == "heartbeat.tick" and e["workflow"] == heartbeat_id
    ]


def bare_heartbeat_id() -> str:
    """The standing heartbeat's workflow id — the one the ensure loop
    keeps running and the platform's own wakes reach."""
    return workflow_id("heartbeat", COMPANY)


def wake_bare_heartbeat() -> None:
    """Nudge the standing heartbeat through the console — the harness's
    stand-in for a change only the outside world saw (an issue filed by
    hand, a PR merged by hand), compressed from the reconcile wait to now.
    A real capability exercised, not a seam: a person sends the same
    nudge, decided and audited under their name."""
    console().wake()


def await_new_ticks(mark: int, n: int, deadline_s: int = 180) -> list[dict[str, Any]]:
    """Block until the standing heartbeat has journaled at least `mark + n`
    tick rows and return the rows past the mark. Count-then-wake
    discipline: take the mark, wake once, wait here before waking again —
    a wake landing mid-tick coalesces, so blind sends under-count."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        ticks = heartbeat_ticks(bare_heartbeat_id())
        if len(ticks) >= mark + n:
            return ticks[mark:]
        time.sleep(3)
    raise AssertionError(
        f"the heartbeat wrote {len(heartbeat_ticks(bare_heartbeat_id()))} ticks"
        f" within {deadline_s}s; needed {mark + n}"
    )


def await_bare_heartbeat_asleep(deadline_s: int = 300) -> None:
    """Block until the company's `heartbeat-{company}` is running and
    parked on its wait, so tick counts taken after this are the module's
    own. Read off the workflow's own `state` query: a profile re-applied
    over itself finds the standing heartbeat already waiting. The parked
    loop answers a wake immediately — "sleeping" means ready, never
    dormant."""
    bare = workflow_id("heartbeat", COMPANY)
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        # Not started yet, or between runs: keep waiting.
        with contextlib.suppress(Exception):
            execution = console().heartbeat()["execution"]
            if execution["status"] == "RUNNING" and execution["state"] == "sleeping":
                return
        time.sleep(3)
    raise AssertionError(
        f"the bare heartbeat {bare} was not asleep within {deadline_s}s of the apply"
    )


def wake_and_await_ticks(n: int = 1) -> list[dict[str, Any]]:
    """The one way a module makes the standing heartbeat look now: count
    its ticks, send the platform's own wake, and return the `n` ticks it
    journals from there — the module's own, never an earlier one's."""
    mark = len(heartbeat_ticks(bare_heartbeat_id()))
    wake_bare_heartbeat()
    return await_new_ticks(mark, n)


def filed_tickets_since(since: str) -> list[int]:
    """The tickets the company filed after `since` (a registry timestamp
    from `db_now`), by number — read from the registry the way the loop
    reads its own writes, so a module never waits on the host's listing
    for a ticket the platform just filed."""
    rows = psql_json(
        "SELECT COALESCE(json_agg(t.number ORDER BY t.number), '[]'::json) "
        "FROM registry.filed_tickets t WHERE t.filed_at > :'since'::timestamptz",
        since=since,
    )
    return [int(n) for n in rows]
