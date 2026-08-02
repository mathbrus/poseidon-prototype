"""Integration tests for the DockerBackend (marked `docker`).

Requires a reachable Docker daemon; builds the sandbox image from
Dockerfile.sandbox, provisions a sandbox from a tarball fixture, talks to the
tool server over the container network, and destroys the sandbox.

Run with: uv run pytest packages/sandbox -m docker
"""

import io
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

docker = pytest.importorskip("docker")

from sandbox.backends.docker import DockerBackend  # noqa: E402
from sandbox.contract import SandboxPolicy  # noqa: E402

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_NETWORK = "poseidon-sbx-test-net"
RUN_ID = "run-docker-backend-test"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


def git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=workspace, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def docker_client() -> "docker.DockerClient":
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # daemon absent/unreachable
        pytest.skip(f"Docker daemon not available: {exc}")
    return client


@pytest.fixture(scope="session")
def sandbox_image(docker_client) -> str:
    tag = "poseidon-sandbox:test"
    docker_client.images.build(
        path=str(REPO_ROOT), dockerfile="Dockerfile.sandbox", tag=tag, rm=True
    )
    return tag


@pytest.fixture
def network(docker_client) -> str:
    net = docker_client.networks.create(TEST_NETWORK, driver="bridge")
    yield TEST_NETWORK
    net.remove()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A git clone as repo.archive would deliver it: base commit + a remote to strip."""
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "hello.txt").write_text("hello\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "remote", "add", "origin", "https://example.com/repo.git")
    return tmp_path


@pytest.fixture
def workspace_tar(workspace: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in workspace.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


def container_ip(docker_client, handle, network: str) -> str:
    container = docker_client.containers.get(handle.container_id)
    return container.attrs["NetworkSettings"]["Networks"][network]["IPAddress"]


def wait_ready(url: str, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.post(f"{url}/files/list", json={"path": "."}, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    pytest.fail(f"tool server at {url} not ready after {timeout_s}s")


def test_provision_serve_destroy(docker_client, sandbox_image, network, workspace, workspace_tar):
    backend = DockerBackend(image=sandbox_image, network=network, client=docker_client)
    handle = backend.provision(RUN_ID, workspace_tar, SandboxPolicy(image=sandbox_image))
    try:
        assert handle.run_id == RUN_ID
        # The handle carries the container's IP on the sandbox network — the
        # host has a route onto local bridge networks, so the test talks to
        # the handle URL directly, exactly as the gateway would.
        ip = container_ip(docker_client, handle, network)
        assert handle.url == f"http://{ip}:8000"

        url = handle.url
        wait_ready(url)

        entries = {
            e["name"]
            for e in httpx.post(f"{url}/files/list", json={"path": "."}).json()["entries"]
        }
        assert {"hello.txt", ".git", ".poseidon-base"} <= entries

        base = httpx.post(f"{url}/files/read", json={"path": ".poseidon-base"}).json()
        assert base["content"].strip() == git(workspace, "rev-parse", "HEAD")

        remotes = httpx.post(f"{url}/exec", json={"cmd": "git remote"}).json()
        assert remotes["exit_code"] == 0
        assert remotes["stdout"].strip() == ""
    finally:
        backend.destroy(handle)

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(handle.container_id)


def test_destroy_is_idempotent(docker_client, sandbox_image, network, workspace_tar):
    backend = DockerBackend(image=sandbox_image, network=network, client=docker_client)
    handle = backend.provision(
        RUN_ID + "-idem", workspace_tar, SandboxPolicy(image=sandbox_image)
    )
    backend.destroy(handle)
    backend.destroy(handle)  # second call must not raise


def test_provision_failure_removes_container(docker_client, sandbox_image, network):
    """A tarball without a git repo fails preparation; no container may leak."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("just-a-file.txt")
        data = b"not a git repo"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    backend = DockerBackend(image=sandbox_image, network=network, client=docker_client)
    with pytest.raises(RuntimeError, match="workspace preparation failed"):
        backend.provision(
            RUN_ID + "-fail", buf.getvalue(), SandboxPolicy(image=sandbox_image)
        )

    leaked = docker_client.containers.list(
        all=True, filters={"name": f"poseidon-sbx-{RUN_ID}-fail"}
    )
    assert leaked == []
