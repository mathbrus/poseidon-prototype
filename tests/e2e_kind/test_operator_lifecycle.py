"""The declarative company lifecycle on kind, and the full revocation
claim.

apply converges a live company (namespace, RBAC, standing agents, served bundle);
a blueprint edit converges as a rollout with no manual steps; deleting the
Company revokes — namespace gone, bundle 404, a restarted OPA activates
nothing for the company, and a credential that worked before the teardown
is refused at the gateway afterwards.

The module finalizer always restores `examples/hello-saas.yaml` and waits
for convergence, so the rest of the suite finds a healthy company whether
these tests pass or fail.
"""

import io
import tarfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    bundle_revision_from_cr,
    mark_drifted,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    gateway_url,
    kubectl,
    port_forward,
    projected_token,
)


@pytest.fixture(scope="module", autouse=True)
def restore_company(stack: None):
    """Whatever happens in here, the suite's later files need the company
    back: re-apply and wait."""
    yield
    restore_pristine()


# -- (a) apply converges -------------------------------------------------------


def test_apply_converges_the_company(stack: None):
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()

    # The namespace is operator-owned and carries its labels; the
    # orchestrator's bounded sandbox RBAC was reconciled into it.
    labels = yaml.safe_load(kubectl("get", "ns", COMPANY_NS, "-o", "yaml"))["metadata"][
        "labels"
    ]
    assert labels["app.kubernetes.io/managed-by"] == "poseidon-operator"
    assert labels["poseidon.dev/company"] == COMPANY_NS
    kubectl("-n", COMPANY_NS, "get", "role", "sandbox-provisioner")
    kubectl("-n", COMPANY_NS, "get", "rolebinding", "sandbox-provisioner")

    # The firing diff, converged: exactly one standing AgentInstance
    # (the PM conductor) — execution is ephemeral crews, so no standing
    # engineer exists to converge.
    instances = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "agentinstances",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).split()
    assert instances == ["pm"], f"standing agents should be the PM only: {instances}"
    deployments = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "deployments",
        "-l",
        "poseidon.dev/kind=standing-agent",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).split()
    assert deployments == ["pm"], f"unexpected standing deployments: {deployments}"

    # Status is really written, not pruned by the CRD's status schema.
    revision = bundle_revision_from_cr()
    assert revision, "Company status.bundleRevision is empty"
    phase = kubectl("get", "company", COMPANY_NS, "-o", "jsonpath={.status.phase}")
    assert phase == "converged"

    # The bundle server serves that exact revision over the OPA bundle API.
    with port_forward("svc/bundle-server", 8081) as port:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/bundles/{COMPANY_NS}/bundle.tar.gz", timeout=30
        )
        assert resp.status_code == 200
        assert resp.headers["etag"] == f'"{revision}"'
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
            assert ".manifest" in tar.getnames()


def test_opa_activated_both_bundles(stack: None):
    """Compiled bundles are the only served policy: the company
    bundle under its own package, the platform bundle under the reserved
    key — disjoint roots, both active, default-deny entrypoints."""
    revision = bundle_revision_from_cr()
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
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
    """Change the PM's model tier, re-apply, observe a rollout with no
    manual steps."""
    document = yaml.safe_load(HELLO_SAAS.read_text())
    assert document["standing"]["pm"]["model_tier"] != "small"
    document["standing"]["pm"]["model_tier"] = "small"
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
        )
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
    agent_token = projected_token(COMPANY_NS, "pm-1")
    mcp = f"{gateway_url(env)}/mcp"
    before = httpx.get(
        mcp, headers={"Authorization": f"Bearer {agent_token}"}, timeout=10
    )
    assert before.status_code != 401, "the pre-teardown credential must work"

    kubectl("delete", "company", COMPANY_NS, "--timeout=120s", timeout=150)
    kubectl("wait", "--for=delete", f"ns/{COMPANY_NS}", "--timeout=180s", timeout=200)
    mark_drifted()  # the cluster no longer matches any applied document

    # Revocation at the delivery layer: the bundle is no longer served.
    with port_forward("svc/bundle-server", 8081) as port:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/bundles/{COMPANY_NS}/bundle.tar.gz", timeout=30
        )
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
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        compiled = httpx.get(
            f"{base}/v1/data/poseidon/authz/hello_saas/decision", timeout=10
        ).json()
        assert "result" not in compiled, "compiled policy survived teardown"
        platform = httpx.get(
            f"{base}/v1/data/poseidon/platform/decision", timeout=10
        ).json()
        assert "result" in platform

    # And at the gateway: the fired agent's ServiceAccount and instance
    # row died with the namespace, so the pre-teardown token is refused
    # once the gateway's short authn cache (60s) drains — a failed
    # post-teardown call, as the lifecycle demands.
    deadline = time.monotonic() + 120
    while True:
        after = httpx.get(
            mcp, headers={"Authorization": f"Bearer {agent_token}"}, timeout=10
        )
        if after.status_code == 401:
            break
        assert time.monotonic() < deadline, (
            f"pre-teardown credential still accepted ({after.status_code})"
        )
        time.sleep(5)
    # The module finalizer re-applies and waits, so the rest of the suite
    # finds a healthy company.
