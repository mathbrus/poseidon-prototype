"""The OpenShell evaluation backend (plan step 24) against a fake client.

No cluster and no OpenShell install: these tests pin the *adapter's*
promises — the declawing (zero credential providers, no inference route),
the contract-v2 lifecycle, and the OCSF ingest mapping. Whether OpenShell
itself honours a spec is the report's business, not a unit test's.
"""

import io
import tarfile
from dataclasses import dataclass, field
from typing import Any

import pytest

from common.audit import AuditEvent
from sandbox.backends.openshell import (
    ContractGap,
    OpenShellBackend,
    UnmappedOcsfClass,
    sandbox_name,
    to_audit_event,
)
from sandbox.contract import SandboxHandle, SandboxPolicy

RUN_ID = "run-01OPENSHELL"


@dataclass
class FakeRef:
    id: str
    name: str
    workspace: str
    status: Any = None
    labels: dict = field(default_factory=dict)


@dataclass
class FakeExecResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeClient:
    """Mirrors the real `SandboxClient` surface we depend on (create /
    wait_ready / exec / delete), including that `exec` takes real stdin
    bytes — the one place OpenShell is nicer than the pods/exec websocket."""

    def __init__(self, exec_result: FakeExecResult | None = None) -> None:
        self.created: list[dict] = []
        self.ready: list[str] = []
        self.execs: list[dict] = []
        self.deleted: list[str] = []
        self._exec_result = exec_result or FakeExecResult()

    def create(self, *, workspace, spec=None, name=None, labels=None):
        self.created.append({"workspace": workspace, "spec": spec,
                             "name": name, "labels": dict(labels or {})})
        return FakeRef(id=f"sbx-{name}", name=name, workspace=workspace)

    def wait_ready(self, sandbox_name, *, workspace, timeout_seconds=300.0):
        self.ready.append(sandbox_name)
        return FakeRef(id=f"sbx-{sandbox_name}", name=sandbox_name,
                       workspace=workspace)

    def exec(self, sandbox_id, command, **kwargs):
        self.execs.append({"sandbox_id": sandbox_id, "command": list(command),
                           **kwargs})
        return self._exec_result

    def delete(self, sandbox_name, *, workspace):
        self.deleted.append(sandbox_name)
        return True


def workspace_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("main.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def policy(**overrides) -> SandboxPolicy:
    values = {"image": "poseidon-sandbox:pinned", "cpu": "500m",
              "memory": "512Mi", "env": {"HTTP_PROXY": "http://run:secret@proxy:3128"}}
    values.update(overrides)
    return SandboxPolicy(**values)


# -- the contract-v2 lifecycle -------------------------------------------------


def test_provision_creates_labels_waits_and_delivers_the_workspace():
    client = FakeClient()
    backend = OpenShellBackend(client, toolserver_url="http://shim:8000",
                               spec_builder=lambda policy: {"stub": True})

    handle = backend.provision(RUN_ID, workspace_tar(), policy())

    (created,) = client.created
    assert created["name"] == sandbox_name(RUN_ID)
    # The run label is what any drain/ingest job correlates by — OCSF
    # records cannot attribute themselves to a Poseidon run.
    assert created["labels"]["poseidon.io/run-id"] == RUN_ID
    assert client.ready == [sandbox_name(RUN_ID)]

    # The workspace went in over real stdin, untouched.
    (delivered,) = client.execs
    assert delivered["stdin"] == workspace_tar()
    assert "tar -x" in " ".join(delivered["command"])

    assert isinstance(handle, SandboxHandle)
    assert handle.run_id == RUN_ID
    assert handle.job_name == sandbox_name(RUN_ID)


def test_destroy_is_idempotent_like_the_default_backend():
    client = FakeClient()
    backend = OpenShellBackend(client, toolserver_url="http://shim:8000",
                               spec_builder=lambda policy: {"stub": True})
    handle = backend.provision(RUN_ID, workspace_tar(), policy())

    backend.destroy(handle)
    backend.destroy(handle)  # teardown runs on every terminal path, twice over

    assert client.deleted == [sandbox_name(RUN_ID)] * 2


def test_failed_workspace_delivery_is_loud():
    client = FakeClient(FakeExecResult(exit_code=2, stderr="tar: short read"))
    backend = OpenShellBackend(client, toolserver_url="http://shim:8000",
                               spec_builder=lambda policy: {"stub": True})

    with pytest.raises(ContractGap, match="workspace delivery failed"):
        backend.provision(RUN_ID, workspace_tar(), policy())


def test_missing_toolserver_url_refuses_rather_than_inventing_one():
    """The evaluation's headline gap: OpenShell publishes no per-sandbox
    address, so C4's toolserver URL cannot come from a SandboxRef. Handing
    back a plausible-looking dead URL would make the integration look
    cheaper than it is."""
    backend = OpenShellBackend(FakeClient())

    with pytest.raises(ContractGap, match="no per-sandbox address"):
        backend.provision(RUN_ID, workspace_tar(), policy())


# -- the declawing -------------------------------------------------------------


def test_spec_carries_no_credential_providers_and_no_inference_route():
    """§4.6: a Poseidon sandbox holds no credential but its own run-scoped
    egress token, and model calls belong to the metered LLM gateway. Both
    of OpenShell's conveniences stay off."""
    openshell = pytest.importorskip(
        "openshell", reason="evaluation extra; not installed on the MVP path"
    )
    from sandbox.backends.openshell.backend import build_spec

    spec = build_spec(policy(), openshell)

    assert not spec.providers, "credential providers must stay empty"
    assert spec.template.image == "poseidon-sandbox:pinned"
    # The §2b egress credential is the one secret that rides in.
    assert spec.environment["HTTP_PROXY"].startswith("http://run:secret@")
    # Filesystem scope is the workspace, and nothing wider.
    assert list(spec.policy.filesystem.read_write) == ["/workspace"]


# -- OCSF ingest ---------------------------------------------------------------


def ocsf(**overrides) -> dict:
    record = {
        "class_uid": 4001,
        "time_dt": "2026-08-06T07:00:00+00:00",
        "disposition_id": 2,
        "duration": 12,
        "dst_endpoint": {"hostname": "exfil.example", "port": 443},
    }
    record.update(overrides)
    return record


def test_ocsf_block_becomes_a_sandbox_infra_deny():
    event = to_audit_event(ocsf(), run_id=RUN_ID, company="hello-saas")

    assert isinstance(event, AuditEvent)
    assert event.source == "sandbox-infra"  # one story, not a parallel silo
    assert event.decision == "deny"
    assert event.tool == "sandbox.network"
    assert event.run_id == RUN_ID
    assert event.company == "hello-saas"
    assert event.ts == "2026-08-06T07:00:00+00:00"  # when it happened
    assert len(event.args_digest) == 64  # digested, never stored raw


def test_ocsf_allow_keeps_its_backend_status():
    event = to_audit_event(ocsf(disposition_id=1), run_id=RUN_ID)
    assert event.decision == "allow"
    assert event.backend_status == "ok"


def test_epoch_timestamps_are_accepted_but_never_invented():
    event = to_audit_event(
        ocsf(time_dt=None, time=1785974400000), run_id=RUN_ID
    )
    assert event.ts.startswith("2026-")

    with pytest.raises(UnmappedOcsfClass, match="timestamp"):
        to_audit_event(ocsf(time_dt=None), run_id=RUN_ID)


def test_an_unmapped_class_is_refused_not_guessed():
    """A silently mislabelled audit row is worse than an admitted gap."""
    with pytest.raises(UnmappedOcsfClass, match="9999"):
        to_audit_event(ocsf(class_uid=9999), run_id=RUN_ID)


def test_the_same_action_digests_alike_across_drains():
    first = to_audit_event(ocsf(duration=12), run_id=RUN_ID)
    second = to_audit_event(ocsf(duration=99), run_id=RUN_ID)
    assert first.args_digest == second.args_digest


# -- "strictly non-gating", enforced rather than promised -----------------------


def test_nothing_on_the_mvp_critical_path_references_openshell():
    """Plan step 24's second done-when. The evaluation track earns its
    keep only if it cannot quietly become load-bearing: no production
    module outside this backend may import it, and the worker must still
    provision through `k8s_jobs`."""
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    hits = subprocess.run(
        ["git", "grep", "-l", "openshell", "--", "packages/*/src", "deploy",
         "tests"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    ).stdout.split()
    offenders = [
        path for path in hits
        if not path.startswith("packages/sandbox/src/sandbox/backends/openshell/")
    ]
    assert offenders == [], f"OpenShell reached the critical path: {offenders}"

    worker = (repo / "packages/orchestrator/src/orchestrator/worker/__main__.py"
              ).read_text()
    assert "K8sJobsBackend" in worker
    assert "openshell" not in worker.lower()
