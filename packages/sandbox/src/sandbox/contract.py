"""SandboxBackend contract — frozen contract C5.

The narrow seam behind which sandbox runtimes are swapped: the PoC ships a
Docker implementation (`sandbox.backends.docker`); OpenShell and K8s-Jobs
backends implement this same contract later.
"""

from typing import Protocol

from pydantic import BaseModel


class SandboxHandle(BaseModel):
    run_id: str
    container_id: str
    url: str


class SandboxBackend(Protocol):
    def provision(self, run_id: str, workspace_tar: bytes) -> SandboxHandle: ...

    def destroy(self, handle: SandboxHandle) -> None: ...
