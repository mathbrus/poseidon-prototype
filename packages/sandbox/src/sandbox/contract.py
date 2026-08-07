"""SandboxBackend contract v2 — frozen contract C5 (overview §4.6, §5.3).

The narrow seam behind which sandbox runtimes are swapped: `k8s_jobs` is the
MVP default.

`provision` gains a compiled policy argument. Until the compiler emits the
infrastructure policy IR (plan step 12), `SandboxPolicy` values are
hand-authored config (sequencing choice 2) — the method set never changes
twice.
"""

from typing import Protocol

from pydantic import BaseModel


class SandboxPolicy(BaseModel):
    """The sandbox projection of the infra-policy IR (§5.4).

    image/toolchain, egress allowlist, filesystem scope, CPU/memory/disk
    caps, wall-clock deadline.
    """

    image: str
    # Host allowlist enforced by the egress proxy (plan step 5); empty means
    # zero egress, exactly as the PoC.
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
    # Per-run values injected into the sandbox environment at provision time.
    # This is the frozen §2b injection point for the per-run egress-proxy
    # credential (populated at plan step 5).
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
