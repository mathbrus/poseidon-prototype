"""The declarative company lifecycle on kind, and the full revocation
claim.

apply converges a live company (namespace, RBAC, standing agents, served bundle
— `converged` is written once OPA serves it); a blueprint edit converges as a
rollout with no manual steps; deleting the Company revokes — the served bundle
becomes a tombstone the running OPA activates before the namespace goes, and
a credential that worked before the teardown is refused at the gateway
afterwards.

The module finalizer always restores `examples/hello-saas.yaml` and waits
for convergence, so the rest of the suite finds a healthy company whether
these tests pass or fail.
"""

import io
import json
import tarfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

from common.policy import COMPANY_BUNDLE, company_decision_path, company_package

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    bundle_revision_from_cr,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    COMPANY,
    COMPANY_NS,
    console,
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

    # The namespace is reconciler-owned and carries its labels; the
    # orchestrator's bounded sandbox RBAC was reconciled into it.
    labels = yaml.safe_load(kubectl("get", "ns", COMPANY_NS, "-o", "yaml"))["metadata"][
        "labels"
    ]
    assert labels["app.kubernetes.io/managed-by"] == "poseidon-reconciler"
    assert labels["poseidon.dev/company"] == COMPANY
    kubectl("-n", COMPANY_NS, "get", "role", "sandbox-provisioner")
    kubectl("-n", COMPANY_NS, "get", "rolebinding", "sandbox-provisioner")

    # The firing diff, converged: exactly one standing AgentInstance
    # (the PM conductor) — execution is ephemeral crews, so no standing
    # implementer exists to converge.
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
    phase = kubectl("get", "company", COMPANY, "-o", "jsonpath={.status.phase}")
    assert phase == "converged"

    # The bundle server serves that exact revision over the OPA bundle API.
    with port_forward("svc/bundle-server", 8081) as port:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/bundles/{COMPANY_BUNDLE}/bundle.tar.gz", timeout=30
        )
        assert resp.status_code == 200
        assert resp.headers["etag"] == f'"{revision}"'
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
            assert ".manifest" in tar.getnames()


def test_opa_activated_both_bundles(stack: None):
    """Compiled bundles are the only served policy: the company
    bundle under its own package, the platform bundle under the reserved
    key — disjoint roots, both active, default-deny entrypoints. No
    waiting: `converged` on the CR is the reconciler's word that OPA
    already serves this revision."""
    revision = bundle_revision_from_cr()
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        # Activation metadata lands in data.system.bundles.<name> (the
        # /v1/status API would need the status plugin enabled).
        manifest = httpx.get(
            f"{base}/v1/data/system/bundles/{COMPANY_BUNDLE}/manifest", timeout=10
        ).json()
        assert manifest.get("result", {}).get("revision") == revision

        company = httpx.get(
            f"{base}/v1/data/{company_decision_path(COMPANY)}", timeout=10
        ).json()
        assert company.get("result") == {"allow": False, "rule": "default_deny"}

        platform = httpx.get(
            f"{base}/v1/data/poseidon/platform/decision", timeout=10
        ).json()
        assert platform.get("result") == {"allow": False, "rule": "default_deny"}


# -- (b) a blueprint edit converges --------------------------------------------


def test_blueprint_edit_rolls_the_pm(stack: None, tmp_path: Path):
    """Change the company's vision, re-apply, observe a rollout with no
    manual steps: what the PM is told about its company rides its pod."""
    document = yaml.safe_load(HELLO_SAAS.read_text())
    vision = "Ship it, but differently."
    assert document["company"]["vision"] != vision
    document["company"]["vision"] = vision
    edited = tmp_path / "hello-saas-new-vision.yaml"
    edited.write_text(yaml.safe_dump(document, sort_keys=False))

    apply_blueprint(edited)
    deadline = time.monotonic() + 180
    while True:
        told = kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "deployment",
            "pm",
            "-o",
            'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="VISION")].value}',
        )
        if told == vision:
            break
        assert time.monotonic() < deadline, f"pm pod template still tells {told!r}"
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

    # The stop is a console act: decided, audited under the owner's name,
    # and the console returns only once the reconciler's teardown is done.
    deleted = console(env).delete_company("lifecycle: revocation proof")
    assert deleted.company == COMPANY and deleted.runs_cancelled == []
    kubectl("wait", "--for=delete", f"ns/{COMPANY_NS}", "--timeout=180s", timeout=200)
    assert kubectl("get", "companies", "-o", "name") == ""

    # Revocation at the delivery layer: the served bundle is a tombstone —
    # the company's roots and no rules under them. The reconciler held the
    # deletion until the running OPA had activated it, so by the time the
    # Company is gone the decision path already answers no document; the
    # platform bundle (the platform's, not the company's) is still served.
    with port_forward("svc/bundle-server", 8081) as port:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/bundles/{COMPANY_BUNDLE}/bundle.tar.gz", timeout=30
        )
        assert resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
            assert tar.getnames() == [".manifest"]
            manifest = json.loads(tar.extractfile(".manifest").read())
        assert manifest["roots"] == [company_package(COMPANY).replace(".", "/")]
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        compiled = httpx.get(
            f"{base}/v1/data/{company_decision_path(COMPANY)}", timeout=10
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
