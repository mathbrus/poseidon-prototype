"""Applying the example blueprints to the cluster, exactly as an operator
of the platform does, and waiting for the company to converge — which
includes policy: OPA must have activated the company's bundle, not just
the pods be Ready."""

import asyncio
import contextlib
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml
from temporalio.client import Client

from orchestrator.workflows.base import workflow_id

from .cluster import COMPANY_NS, kubectl, port_forward, temporal_port_forward

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
HELLO_SAAS = EXAMPLES / "hello-saas.yaml"
DELEGATED = EXAMPLES / "hello-saas-delegated.yaml"

# The examples declare the waits a real company gives its humans (a working
# day at the merge gate, an hour at a budget stop). The suite waits those
# gates out to watch them escalate and expire, so every document it applies
# carries these instead: escalation lands at half a minute, and the budget
# approve path has time to re-apply a blueprint before its pause ends.
E2E_GATE_MINUTES = 1
E2E_PAUSE_MINUTES = 2


def derived_blueprint(base: Path, mutate: Callable[[dict], None], label: str) -> Path:
    """A harness-local variant of an example blueprint: the example's
    document with one declared difference, written to a temp file for
    `apply_blueprint`. The module story stays "the example, minus the
    piece this module's explicit drive must not race"."""
    document = yaml.safe_load(base.read_text())
    mutate(document)
    path = Path(tempfile.mkdtemp(prefix="poseidon-bp-")) / f"{base.stem}-{label}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def heartbeat_variant() -> Path:
    """`hello-saas.yaml` plus the delegated example's `heartbeat` section:
    a company that starts its own runs while a human still answers every
    merge — the middle rung between the two shipped examples, for modules
    that need the heartbeat without the delegated gate."""

    def add_heartbeat(document: dict) -> None:
        document["heartbeat"] = yaml.safe_load(DELEGATED.read_text())["heartbeat"]

    return derived_blueprint(HELLO_SAAS, add_heartbeat, "heartbeat")


def delegated_without_heartbeat() -> Path:
    """The delegated example minus its `heartbeat` section, for modules
    that drive implement and review runs themselves: with no section
    declared the ensure loop starts nothing and wakes land on nobody, so
    the module's explicit arc cannot be raced for its concurrency slot."""

    def drop(document: dict) -> None:
        document.pop("heartbeat", None)

    return derived_blueprint(DELEGATED, drop, "no-heartbeat")


def heartbeat_implement_only() -> Path:
    """The heartbeat example with `may_start: [implement]`: the company
    still runs its own backlog, but a delivered PR waits for the outside
    world instead of being reviewed on the next wake — the declaration a
    module needs to watch what happens to a PR only a human settles."""

    def implement_only(document: dict) -> None:
        document["heartbeat"]["may_start"] = ["implement"]

    return derived_blueprint(heartbeat_variant(), implement_only, "implement-only")


def suite_document(path: Path) -> Path:
    """The document at `path` with the suite's gate waits declared in place
    of the example's human-scale ones — every gate's `deadline_minutes` and
    the budget `pause_minutes`. Nothing else changes: the suite applies the
    example's authority, on the suite's clock."""
    document = yaml.safe_load(path.read_text())
    for node in (document.get("workflows") or {}).values():
        for gate in (node.get("gates") or {}).values():
            gate["deadline_minutes"] = E2E_GATE_MINUTES
    document["budgets"]["per_run"]["pause_minutes"] = E2E_PAUSE_MINUTES
    out = Path(tempfile.mkdtemp(prefix="poseidon-bp-")) / f"{path.stem}-e2e.yaml"
    out.write_text(yaml.safe_dump(document, sort_keys=False))
    return out


def apply_blueprint(path: Path) -> None:
    """`poseidon blueprint apply` of the suite's version of `path` (see
    `suite_document`). The registry may be unreachable from the host —
    apply degrades to diffing against nothing and still submits the CRs."""
    path = suite_document(path)
    proc = subprocess.run(
        ["uv", "run", "poseidon", "blueprint", "apply", str(path), "--yes"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"blueprint apply failed: {proc.stderr}"


def wait_for_convergence(timeout_s: int = 300) -> None:
    """The company namespace is Active, its one standing agent (the PM) is
    rolled out, and OPA serves the company's decision document. The last
    part matters after a teardown + re-apply: OPA needs a poll cycle
    (5-15s) to activate the bundle, and a test that runs before it would
    land in a `no_policy_installed` window."""
    kubectl(
        "wait",
        "--for=jsonpath={.status.phase}=Active",
        f"ns/{COMPANY_NS}",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    kubectl(
        "-n",
        COMPANY_NS,
        "rollout",
        "status",
        "deployment/pm",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    with port_forward("svc/opa", 8181) as port:
        deadline = time.monotonic() + 120
        while True:
            decision = httpx.get(
                f"http://127.0.0.1:{port}/v1/data/poseidon/authz/hello_saas/decision",
                timeout=10,
            ).json()
            if "result" in decision:
                return
            assert time.monotonic() < deadline, (
                "OPA never activated the re-applied company bundle"
            )
            time.sleep(3)


def bundle_revision_from_cr() -> str:
    return kubectl(
        "get", "company", COMPANY_NS, "-o", "jsonpath={.status.bundleRevision}"
    )


def quiesce_heartbeat() -> None:
    """Terminate the ensure loop's bare `heartbeat-{company}` heartbeat after
    the pristine blueprint is back. Revocation-by-diff stops it on its own
    — the product claim, pinned at the workflow layer — but only at
    its next tick, up to `reconcile_minutes` away. The campsite rule wants
    the quiet now: call this after the pristine apply, so the ensure loop has
    nothing to restart."""

    async def terminate() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("heartbeat", COMPANY_NS))
            with contextlib.suppress(Exception):
                await handle.terminate("suite teardown: pristine blueprint restored")

    asyncio.run(terminate())


def restore_pristine() -> None:
    """Back to `hello-saas.yaml`, converged and quiet — what every module
    that applied another profile owes the modules after it. Always
    re-applies and waits for convergence (OPA bundle activation included):
    a restore that skipped the wait could hand the next module a company
    that never re-converged."""
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()
    quiesce_heartbeat()
