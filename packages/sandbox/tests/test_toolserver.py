import base64
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sandbox.toolserver import create_app

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",  # keep user gitconfig out of the tests
}


def git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A git repo shaped like a provisioned workspace: one base commit + .poseidon-base."""
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "hello.txt").write_text("hello\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / ".poseidon-base").write_text(base_sha + "\n")
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace=workspace))


# --- /files/read -----------------------------------------------------------


def test_read_file(client: TestClient):
    resp = client.post("/files/read", json={"path": "hello.txt"})
    assert resp.status_code == 200
    assert resp.json() == {"content": "hello\n"}


def test_read_missing_file_404(client: TestClient):
    assert client.post("/files/read", json={"path": "nope.txt"}).status_code == 404


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "pkg/../../escape"])
def test_read_traversal_rejected(client: TestClient, path: str):
    assert client.post("/files/read", json={"path": path}).status_code == 400


# --- /files/write ----------------------------------------------------------


def test_write_file_creates_parents(client: TestClient, workspace: Path):
    resp = client.post(
        "/files/write", json={"path": "new/dir/file.txt", "content": "data"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert (workspace / "new" / "dir" / "file.txt").read_text() == "data"


def test_write_traversal_rejected(client: TestClient, tmp_path: Path):
    resp = client.post("/files/write", json={"path": "../evil.txt", "content": "x"})
    assert resp.status_code == 400
    assert not (tmp_path.parent / "evil.txt").exists()


def test_write_symlink_escape_rejected(
    client: TestClient, workspace: Path, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside")
    (workspace / "link").symlink_to(outside)
    resp = client.post("/files/write", json={"path": "link/evil.txt", "content": "x"})
    assert resp.status_code == 400
    assert not (outside / "evil.txt").exists()


# --- /files/list -----------------------------------------------------------


def test_list_dir(client: TestClient):
    resp = client.post("/files/list", json={"path": "."})
    assert resp.status_code == 200
    entries = {e["name"]: e["type"] for e in resp.json()["entries"]}
    assert entries["hello.txt"] == "file"
    assert entries["pkg"] == "dir"
    assert entries[".git"] == "dir"


def test_list_missing_dir_404(client: TestClient):
    assert client.post("/files/list", json={"path": "nope"}).status_code == 404


def test_list_traversal_rejected(client: TestClient):
    assert client.post("/files/list", json={"path": "../"}).status_code == 400


# --- /exec -------------------------------------------------------------------


def test_exec_runs_in_workspace(client: TestClient, workspace: Path):
    resp = client.post("/exec", json={"cmd": "pwd && cat hello.txt"})
    body = resp.json()
    assert body["exit_code"] == 0
    assert str(workspace) in body["stdout"]
    assert "hello" in body["stdout"]


def test_exec_reports_failure_and_stderr(client: TestClient):
    body = client.post("/exec", json={"cmd": "echo oops >&2; exit 3"}).json()
    assert body["exit_code"] == 3
    assert "oops" in body["stderr"]


def test_exec_timeout(client: TestClient):
    body = client.post("/exec", json={"cmd": "sleep 5", "timeout_s": 0.2}).json()
    assert body["exit_code"] == 124
    assert "timed out" in body["stderr"]


# --- /tests -------------------------------------------------------------------


def test_tests_endpoint_uses_test_cmd_env(client: TestClient, monkeypatch):
    monkeypatch.setenv("TEST_CMD", "echo running-custom-tests")
    body = client.post("/tests", json={}).json()
    assert body["exit_code"] == 0
    assert "running-custom-tests" in body["stdout"]


def test_tests_endpoint_default_is_pytest(
    client: TestClient, monkeypatch, workspace: Path
):
    monkeypatch.delenv("TEST_CMD", raising=False)
    (workspace / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    body = client.post("/tests", json={}).json()
    assert body["exit_code"] == 0
    assert "1 passed" in body["stdout"]


def test_tests_endpoint_scopes_to_named_paths(
    client: TestClient, monkeypatch, workspace: Path
):
    """The contract-verify call: `paths` pins the run to named test paths,
    so the rest of the suite — red or green — never runs."""
    monkeypatch.delenv("TEST_CMD", raising=False)
    contract = workspace / "tests" / "contract"
    contract.mkdir(parents=True)
    (contract / "test_c.py").write_text("def test_c():\n    assert True\n")
    (workspace / "test_red.py").write_text("def test_red():\n    assert False\n")
    body = client.post("/tests", json={"paths": ["tests/contract"]}).json()
    assert body["exit_code"] == 0
    assert "1 passed" in body["stdout"]


def test_tests_endpoint_missing_named_path_is_red(
    client: TestClient, monkeypatch, workspace: Path
):
    """An empty or absent contract tree is a red fact, not a silent pass:
    pytest exits nonzero on a named path it cannot collect."""
    monkeypatch.delenv("TEST_CMD", raising=False)
    body = client.post("/tests", json={"paths": ["tests/contract"]}).json()
    assert body["exit_code"] != 0


def test_tests_endpoint_refuses_escaping_paths(client: TestClient):
    resp = client.post("/tests", json={"paths": ["../../etc"]})
    assert resp.status_code == 400


# --- /bundle -------------------------------------------------------------------


def make_feature_commit(workspace: Path) -> None:
    git(workspace, "checkout", "-b", "feature/change")
    (workspace / "feature.txt").write_text("new\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "feature change")


def test_bundle_returns_verifiable_bundle(
    client: TestClient, workspace: Path, tmp_path_factory
):
    make_feature_commit(workspace)
    resp = client.post("/bundle", json={"branch": "feature/change"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_ref"] == (workspace / ".poseidon-base").read_text().strip()

    bundle_path = tmp_path_factory.mktemp("bundle") / "change.bundle"
    bundle_path.write_bytes(base64.b64decode(body["bundle_b64"]))
    git(workspace, "bundle", "verify", str(bundle_path))
    listed = git(workspace, "bundle", "list-heads", str(bundle_path))
    assert "refs/heads/feature/change" in listed


def test_bundle_unknown_branch_400(client: TestClient):
    resp = client.post("/bundle", json={"branch": "feature/does-not-exist"})
    assert resp.status_code == 400
    assert "git bundle failed" in resp.json()["detail"]


def test_bundle_missing_base_file_400(client: TestClient, workspace: Path):
    make_feature_commit(workspace)
    (workspace / ".poseidon-base").unlink()
    resp = client.post("/bundle", json={"branch": "feature/change"})
    assert resp.status_code == 400
    assert ".poseidon-base" in resp.json()["detail"]
