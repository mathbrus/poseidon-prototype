"""SandboxBackend contract — the frozen sandbox seam.

The narrow seam behind which sandbox runtimes are swapped: `k8s_jobs` is
the default. `provision` takes a compiled policy argument; `SandboxPolicy`
values come from the applied blueprint's infrastructure policy IR.
"""

from typing import Protocol

from pydantic import BaseModel


class SandboxPolicy(BaseModel):
    """The sandbox projection of the infra-policy IR.

    image/toolchain, egress allowlist, filesystem scope, CPU/memory/disk
    caps, wall-clock deadline.
    """

    image: str
    # Host allowlist enforced by the egress proxy; empty means zero egress.
    egress_allow: list[str] = []
    # Filesystem scope: the toolserver confines every operation to this
    # directory (its WORKSPACE_DIR).
    workspace_dir: str = "/workspace"
    # Kubernetes resource quantities; disk maps to ephemeral-storage.
    cpu: str = "1"
    memory: str = "1Gi"
    disk: str = "2Gi"
    # Wall-clock deadline — the Job's activeDeadlineSeconds; the runaway
    # backstop beneath any per-call timeout.
    deadline_s: int = 3600
    # Per-run values injected into the sandbox environment at provision time
    # — the injection point for the per-run egress-proxy credential.
    env: dict[str, str] = {}


class SandboxHandle(BaseModel):
    run_id: str
    url: str
    # k8s_jobs backend: where the Job lives and which pod serves the run.
    namespace: str | None = None
    job_name: str | None = None
    pod_name: str | None = None


class SandboxBackend(Protocol):
    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle: ...

    def destroy(self, handle: SandboxHandle) -> None: ...
