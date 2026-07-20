"""Docker implementation of the SandboxBackend contract (C5).

Spawns one container per run from the prebaked sandbox image (see
Dockerfile.sandbox) on the zero-egress sandbox network, unpacks the workspace
tarball into /workspace, strips git remotes, and records the base ref that
the tool server's /bundle endpoint later reads from `.poseidon-base`.
"""

import docker
import docker.errors

from common.telemetry import get_logger
from sandbox.contract import SandboxHandle

DEFAULT_IMAGE = "poseidon-sandbox"
DEFAULT_NETWORK = "sandboxnet"
TOOLSERVER_PORT = 8000
WORKSPACE = "/workspace"

# Runs once after the workspace is unpacked: no remote may survive inside the
# sandbox, and the base ref must be recorded for later `git bundle` calls.
_PREPARE_CMD = (
    "set -e; "
    "git remote | xargs -r -n1 git remote remove; "
    "git rev-parse HEAD > .poseidon-base"
)


class DockerBackend:
    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        network: str = DEFAULT_NETWORK,
        client: docker.DockerClient | None = None,
    ) -> None:
        self._client = client or docker.from_env()
        self._image = image
        self._network = network
        self._log = get_logger("sandbox.backends.docker")

    def provision(self, run_id: str, workspace_tar: bytes) -> SandboxHandle:
        name = f"poseidon-sbx-{run_id}"
        container = self._client.containers.create(
            self._image,
            name=name,
            network=self._network,
            labels={"poseidon.run_id": run_id},
        )
        try:
            container.put_archive(WORKSPACE, workspace_tar)
            container.start()
            exit_code, output = container.exec_run(
                ["sh", "-c", _PREPARE_CMD], workdir=WORKSPACE
            )
            if exit_code != 0:
                raise RuntimeError(
                    "workspace preparation failed: "
                    + output.decode(errors="replace").strip()
                )
        except Exception:
            container.remove(force=True)
            raise
        handle = SandboxHandle(
            run_id=run_id,
            container_id=container.id,
            # The container name resolves via Docker DNS for peers on the
            # sandbox network (i.e. the gateway).
            url=f"http://{name}:{TOOLSERVER_PORT}",
        )
        self._log.info(
            "sandbox provisioned",
            extra={"run_id": run_id, "container_id": container.id, "url": handle.url},
        )
        return handle

    def destroy(self, handle: SandboxHandle) -> None:
        try:
            container = self._client.containers.get(handle.container_id)
        except docker.errors.NotFound:
            return
        container.remove(force=True)
        self._log.info(
            "sandbox destroyed",
            extra={"run_id": handle.run_id, "container_id": handle.container_id},
        )
