"""Step 10 done-when — and, since the step-12 cutover, the full revocation
claim: the declarative company lifecycle on kind.

apply converges a live company (namespace, RBAC, roster, served bundle);
a blueprint edit converges as a rollout with no manual steps; deleting the
Company revokes — namespace gone, bundle 404, a restarted OPA activates
nothing for the company, and a credential that worked before the teardown
is refused at the gateway afterwards (the post-teardown denial that was
deliberately unclaimable at step 10, when the gateway still queried the
file-loaded poc.rego).

The module finalizer always re-applies `examples/hello-saas.yaml` and waits
for convergence, so the rest of the suite finds a healthy company whether
these tests pass or fail.
"""

import contextlib
import io
import socket
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

from .conftest import COMPANY_NS, PLATFORM_NS, REPO_ROOT

HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"
BUNDLE_LOCAL_PORT = 18081
OPA_LOCAL_PORT = 18181


# -- plumbing ----------------------------------------------------------------


@contextlib.contextmanager
def port_forward(service: str, local: int, remote: int):
    proc = subprocess.Popen(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "port-forward",
            f"svc/{service}",
            f"{local}:{remote}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail(f"port-forward to {service} exited early")
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail(f"port-forward to {service} never came up")
                time.sleep(0.5)
        yield f"http://127.0.0.1:{local}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def kubectl(
    *args: str, check: bool = True, timeout: int = 60
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout
    )
    if check:
        assert proc.returncode == 0, f"kubectl {' '.join(args)}: {proc.stderr}"
    return proc


def apply_blueprint(path: Path) -> None:
    """Exactly what an operator of the platform runs. The registry may be
    unreachable from the host — apply degrades to diffing against nothing
    and still submits the CRs."""
    proc = subprocess.run(
        ["uv", "run", "poseidon", "blueprint", "apply", str(path), "--yes"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"blueprint apply failed: {proc.stderr}"


def wait_for_convergence(timeout_s: int = 300) -> None:
    kubectl(
        "wait",
        "--for=jsonpath={.status.phase}=Active",
        f"ns/{COMPANY_NS}",
        f"--timeout={timeout_s}s",
    )
    # The lean roster: the PM is the only standing hire.
    for hire in ("pm",):
        kubectl(
            "-n",
            COMPANY_NS,
            "rollout",
            "status",
            f"deployment/{hire}",
            f"--timeout={timeout_s}s",
            timeout=timeout_s + 30,
        )
    # Converged includes *policy*: after a teardown + re-apply (and
    # possibly an OPA restart), OPA needs a poll cycle (5-15s) to activate
    # the company bundle. Tests that run right after a finalizer re-apply
    # otherwise land in a no_policy_installed window — a real flake that
    # bit both test_pm and the crew suite.
    with port_forward("opa", OPA_LOCAL_PORT, 8181) as base:
        deadline = time.monotonic() + 120
        while True:
            decision = httpx.get(
                f"{base}/v1/data/poseidon/authz/hello_saas/decision",
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
        "get",
        "company",
        COMPANY_NS,
        "-o",
        "jsonpath={.status.bundleRevision}",
    ).stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def restore_company(stack: None):
    """Whatever happens in here, the suite's later files need the company
    back: re-apply and wait."""
    yield
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()


# -- (a) apply converges -------------------------------------------------------


def test_apply_converges_the_company(stack: None):
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()

    # The namespace is operator-owned and carries its labels; the
    # orchestrator's bounded sandbox RBAC was reconciled into it.
    labels = yaml.safe_load(kubectl("get", "ns", COMPANY_NS, "-o", "yaml").stdout)[
        "metadata"
    ]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == "poseidon-operator"
    assert labels["poseidon.dev/company"] == COMPANY_NS
    kubectl("-n", COMPANY_NS, "get", "role", "sandbox-provisioner")
    kubectl("-n", COMPANY_NS, "get", "rolebinding", "sandbox-provisioner")

    # The step-19 firing diff, converged: exactly one standing AgentInstance
    # (the PM conductor) — execution is ephemeral crews, so no standing
    # engineer exists to converge.
    instances = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "agentinstances",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).stdout.split()
    assert instances == ["pm"], f"standing roster should be the PM only: {instances}"
    deployments = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "deployments",
        "-l",
        "poseidon.dev/kind=standing-agent",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).stdout.split()
    assert deployments == ["pm"], f"unexpected standing deployments: {deployments}"

    # Status is really written (the pruned-by-schema regression).
    revision = bundle_revision_from_cr()
    assert revision, "Company status.bundleRevision is empty"
    phase = kubectl(
        "get", "company", COMPANY_NS, "-o", "jsonpath={.status.phase}"
    ).stdout.strip()
    assert phase == "converged"

    # The bundle server serves that exact revision over the OPA bundle API.
    with port_forward("bundle-server", BUNDLE_LOCAL_PORT, 8081) as base:
        resp = httpx.get(f"{base}/bundles/{COMPANY_NS}/bundle.tar.gz", timeout=30)
        assert resp.status_code == 200
        assert resp.headers["etag"] == f'"{revision}"'
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
            assert ".manifest" in tar.getnames()


def test_opa_activated_both_bundles(stack: None):
    """Compiled bundles are the only served policy (step 12): the company
    bundle under its own package, the platform bundle under the reserved
    key — disjoint roots, both active, default-deny entrypoints."""
    revision = bundle_revision_from_cr()
    with port_forward("opa", OPA_LOCAL_PORT, 8181) as base:
        deadline = time.monotonic() + 120  # OPA polls every 5-15s
        while True:
            # Activation metadata lands in data.system.bundles.<name>
            # (the /v1/status API would need the status plugin enabled).
            manifest = httpx.get(
                f"{base}/v1/data/system/bundles/{COMPANY_NS}/manifest",
                timeout=10,
            ).json()
            active = manifest.get("result", {}).get("revision")
            if active == revision:
                break
            assert time.monotonic() < deadline, (
                f"OPA never activated revision {revision!r}; manifest: {active!r}"
            )
            time.sleep(3)

        company = httpx.get(
            f"{base}/v1/data/poseidon/authz/hello_saas/decision", timeout=10
        ).json()
        assert company.get("result") == {"allow": False, "rule": "default_deny"}

        platform = httpx.get(
            f"{base}/v1/data/poseidon/platform/decision", timeout=10
        ).json()
        assert platform.get("result") == {"allow": False, "rule": "default_deny"}


# -- (b) a blueprint edit converges --------------------------------------------


def test_blueprint_edit_rolls_the_pm(stack: None, tmp_path: Path):
    """The plan's named example: change the PM's model tier, re-apply,
    observe a rollout with no manual steps."""
    document = yaml.safe_load(HELLO_SAAS.read_text())
    assert document["departments"]["roles"]["pm"]["model_tier"] != "small"
    document["departments"]["roles"]["pm"]["model_tier"] = "small"
    edited = tmp_path / "hello-saas-small-pm.yaml"
    edited.write_text(yaml.safe_dump(document, sort_keys=False))

    apply_blueprint(edited)
    deadline = time.monotonic() + 180
    while True:
        tier = kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "deployment",
            "pm",
            "-o",
            r"jsonpath={.spec.template.metadata.labels['poseidon\.dev/model-tier']}",
        ).stdout.strip()
        if tier == "small":
            break
        assert time.monotonic() < deadline, f"pm pod template still at {tier!r}"
        time.sleep(3)
    kubectl(
        "-n",
        COMPANY_NS,
        "rollout",
        "status",
        "deployment/pm",
        "--timeout=180s",
        timeout=200,
    )
    # The module finalizer re-applies the pristine blueprint.


# -- (c) delete revokes ---------------------------------------------------------


def test_company_delete_revokes(stack: None, env: dict):
    # A credential that provably works before the teardown: the standing
    # PM's projected token, exercised through the gateway (the deny is
    # policy answering — authenticated, no binding for this run id).
    from .test_registry_durability import projected_token

    agent_token = projected_token(COMPANY_NS, "pm-1")
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    before = httpx.get(
        f"{gateway_url}/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        timeout=10,
    )
    assert before.status_code != 401, "the pre-teardown credential must work"

    kubectl("delete", "company", COMPANY_NS, "--timeout=120s", timeout=150)
    kubectl("wait", "--for=delete", f"ns/{COMPANY_NS}", "--timeout=180s", timeout=200)

    # Revocation at the delivery layer: the bundle is no longer served.
    with port_forward("bundle-server", BUNDLE_LOCAL_PORT, 8081) as base:
        resp = httpx.get(f"{base}/bundles/{COMPANY_NS}/bundle.tar.gz", timeout=30)
        assert resp.status_code == 404

    # Across an OPA restart: a fresh OPA re-polls, gets the 404, and
    # activates nothing for the company — the platform bundle (which is
    # the platform's, not the company's) is still served.
    kubectl(
        "-n", PLATFORM_NS, "delete", "pod", "-l", "app=opa", "--wait=true", timeout=120
    )
    kubectl(
        "-n",
        PLATFORM_NS,
        "rollout",
        "status",
        "deployment/opa",
        "--timeout=180s",
        timeout=200,
    )
    with port_forward("opa", OPA_LOCAL_PORT, 8181) as base:
        compiled = httpx.get(
            f"{base}/v1/data/poseidon/authz/hello_saas/decision", timeout=10
        ).json()
        assert "result" not in compiled, "compiled policy survived teardown"
        platform = httpx.get(
            f"{base}/v1/data/poseidon/platform/decision", timeout=10
        ).json()
        assert "result" in platform

    # And at the gateway (claimable since step 12): the fired agent's
    # ServiceAccount and instance row died with the namespace, so the
    # pre-teardown token is refused once the gateway's short authn cache
    # (60s) drains — a failed post-teardown call, per the step-10 done-when.
    deadline = time.monotonic() + 120
    while True:
        after = httpx.get(
            f"{gateway_url}/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            timeout=10,
        )
        if after.status_code == 401:
            break
        assert time.monotonic() < deadline, (
            f"pre-teardown credential still accepted ({after.status_code})"
        )
        time.sleep(5)
    # The module finalizer re-applies and waits, so the rest of the suite
    # finds a healthy company.
