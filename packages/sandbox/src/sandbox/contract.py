"""SandboxBackend contract — the seam between the compiled policy and a runtime.

One implementation exists today (`k8s_jobs`); the Protocol is kept because
it is what the compiler's infrastructure IR targets. `provision` takes a
compiled policy argument; `SandboxPolicy` values come from the applied
blueprint's infrastructure policy IR.
"""

from typing import Protocol

from pydantic import BaseModel


class SandboxPolicy(BaseModel):
    """The sandbox projection of the infra-policy IR: image, filesystem
    scope, CPU/memory/disk caps, wall-clock deadline. Every field is set by
    the compiled IR; the egress allowlist is the proxy's, not the pod's.
    """

    image: str
    # Filesystem scope: the toolserver confines every operation to this
    # directory (its WORKSPACE_DIR).
    workspace_dir: str
    # Kubernetes resource quantities; disk maps to ephemeral-storage.
    cpu: str
    memory: str
    disk: str
    # Wall-clock deadline — the Job's activeDeadlineSeconds; the runaway
    # backstop beneath any per-call timeout.
    deadline_s: int
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
