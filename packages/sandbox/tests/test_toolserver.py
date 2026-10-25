import base64
from pathlib import Path

import pytest
from conftest import git
from fastapi.testclient import TestClient

from sandbox.toolserver import create_app


@pytest.fixture
def client(workspace: Path, monkeypatch) -> TestClient:
    """The tool server over the workspace, running its default test
    command whatever the environment running the suite sets: the command
    is read once, when the app is built."""
    monkeypatch.delenv("TEST_CMD", raising=False)
    return TestClient(create_app(workspace=workspace))


# --- /files/read -----------------------------------------------------------


def test_read_file(client: TestClient):
    resp = client.post("/files/read", json={"path": "hello.txt"})
    assert resp.status_code == 200
    assert resp.json() == {"content": "hello\n", "start_line": 1, "total_lines": 1}


def test_read_file_without_a_range_is_capped(client: TestClient, workspace: Path):
    """A whole-file read returns at most READ_LINE_LIMIT lines; the total
    says there is more, to read on in ranges."""
    from sandbox.toolserver.app import READ_LINE_LIMIT

    (workspace / "big.txt").write_text("".join(f"{n}\n" for n in range(5000)))
    body = client.post("/files/read", json={"path": "big.txt"}).json()
    assert body["content"].count("\n") == READ_LINE_LIMIT
    assert body["content"].startswith("0\n")
    assert body["total_lines"] == 5000
    tail = client.post(
        "/files/read", json={"path": "big.txt", "view_range": [4999, -1]}
    ).json()
    assert tail["content"] == "4998\n4999\n"


def test_read_file_view_range(client: TestClient, workspace: Path):
    (workspace / "many.txt").write_text("a\nb\nc\nd\n")
    resp = client.post("/files/read", json={"path": "many.txt", "view_range": [2, 3]})
    assert resp.json() == {"content": "b\nc\n", "start_line": 2, "total_lines": 4}
    tail = client.post("/files/read", json={"path": "many.txt", "view_range": [3, -1]})
    assert tail.json()["content"] == "c\nd\n"
    for bad in ([0, 2], [3, 2], [9, 9]):
        resp = client.post("/files/read", json={"path": "many.txt", "view_range": bad})
        assert resp.status_code == 400, bad


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


# --- /files/edit -----------------------------------------------------------


def test_edit_str_replace_exactly_once(client: TestClient, workspace: Path):
    (workspace / "pkg" / "mod.py").write_text("x = 1\ny = 2\nz = 3\n")
    resp = client.post(
        "/files/edit",
        json={
            "path": "pkg/mod.py",
            "command": "str_replace",
            "old_str": "y = 2",
            "new_str": "y = 20\nw = 4",
        },
    )
    assert resp.status_code == 200, resp.text
    assert (workspace / "pkg" / "mod.py").read_text() == "x = 1\ny = 20\nw = 4\nz = 3\n"
    snippet = resp.json()["snippet"]
    assert "     2\ty = 20\n" in snippet and "     3\tw = 4\n" in snippet


def test_edit_str_replace_refuses_missing_or_ambiguous(
    client: TestClient, workspace: Path
):
    (workspace / "pkg" / "mod.py").write_text("a\nb\na\n")
    missing = client.post(
        "/files/edit",
        json={
            "path": "pkg/mod.py",
            "command": "str_replace",
            "old_str": "q",
            "new_str": "",
        },
    )
    assert missing.status_code == 400 and "not found" in missing.json()["detail"]
    twice = client.post(
        "/files/edit",
        json={
            "path": "pkg/mod.py",
            "command": "str_replace",
            "old_str": "a",
            "new_str": "",
        },
    )
    assert twice.status_code == 400 and "2 times" in twice.json()["detail"]
    no_old = client.post(
        "/files/edit",
        json={"path": "pkg/mod.py", "command": "str_replace", "new_str": ""},
    )
    assert no_old.status_code == 400
    assert (workspace / "pkg" / "mod.py").read_text() == "a\nb\na\n"  # untouched


@pytest.mark.parametrize(
    ("line", "expected"),
    [(0, "new\na\nb\n"), (1, "a\nnew\nb\n"), (2, "a\nb\nnew\n")],
)
def test_edit_insert_after_line(
    client: TestClient, workspace: Path, line: int, expected: str
):
    (workspace / "pkg" / "mod.py").write_text("a\nb\n")
    resp = client.post(
        "/files/edit",
        json={
            "path": "pkg/mod.py",
            "command": "insert",
            "insert_line": line,
            "new_str": "new",
        },
    )
    assert resp.status_code == 200, resp.text
    assert (workspace / "pkg" / "mod.py").read_text() == expected


def test_edit_insert_rejects_bad_line(client: TestClient, workspace: Path):
    for line in (-1, 3):
        resp = client.post(
            "/files/edit",
            json={
                "path": "hello.txt",
                "command": "insert",
                "insert_line": line,
                "new_str": "x",
            },
        )
        assert resp.status_code == 400, line
    no_line = client.post(
        "/files/edit", json={"path": "hello.txt", "command": "insert", "new_str": "x"}
    )
    assert no_line.status_code == 400


def test_edit_missing_file_404(client: TestClient):
    resp = client.post(
        "/files/edit",
        json={
            "path": "nope.py",
            "command": "str_replace",
            "old_str": "a",
            "new_str": "b",
        },
    )
    assert resp.status_code == 404


def test_edit_traversal_and_symlink_escape_rejected(
    client: TestClient, workspace: Path, tmp_path: Path
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret\n")
    (workspace / "link.txt").symlink_to(outside)
    for path in ("../../etc/passwd", "link.txt"):
        resp = client.post(
            "/files/edit",
            json={"path": path, "command": "str_replace", "old_str": "s", "new_str": "x"},
        )
        assert resp.status_code == 400, path
    assert outside.read_text() == "secret\n"


# --- /files/list -----------------------------------------------------------


def test_list_dir_is_the_tree_as_git_sees_it(client: TestClient, workspace: Path):
    """Recursive, workspace-relative, untracked files in, ignored and
    deleted files out — `.git` and the platform's own marker never show."""
    (workspace / "pkg" / "new.py").write_text("y = 2\n")
    (workspace / "pkg" / "__pycache__").mkdir()
    (workspace / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (workspace / "hello.txt").unlink()
    resp = client.post("/files/list", json={"path": "."})
    assert resp.status_code == 200
    assert resp.json() == {"files": ["pkg/mod.py", "pkg/new.py"], "omitted": 0}
    sub = client.post("/files/list", json={"path": "pkg"}).json()
    assert sub["files"] == ["pkg/mod.py", "pkg/new.py"]


def test_list_dir_is_capped(client: TestClient, workspace: Path):
    from sandbox.toolserver.app import LIST_LIMIT

    many = workspace / "many"
    many.mkdir()
    for n in range(LIST_LIMIT + 7):
        (many / f"f{n:04d}.txt").write_text("")
    body = client.post("/files/list", json={"path": "many"}).json()
    assert len(body["files"]) == LIST_LIMIT
    assert body["omitted"] == 7


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


def test_tests_endpoint_uses_test_cmd_env(workspace: Path, monkeypatch):
    # The command is read once, when the app is built — the sandbox Job's
    # env is fixed for its lifetime — so set it before building.
    monkeypatch.setenv("TEST_CMD", "echo running-custom-tests")
    client = TestClient(create_app(workspace=workspace))
    body = client.post("/tests", json={}).json()
    assert body["exit_code"] == 0
    assert "running-custom-tests" in body["stdout"]


def test_tests_endpoint_scopes_to_named_paths(client: TestClient, workspace: Path):
    """The contract-verify call: `paths` pins the run to named test paths,
    so the rest of the suite — red or green — never runs."""
    contract = workspace / "tests" / "contract"
    contract.mkdir(parents=True)
    (contract / "test_c.py").write_text("def test_c():\n    assert True\n")
    (workspace / "test_red.py").write_text("def test_red():\n    assert False\n")
    body = client.post("/tests", json={"paths": ["tests/contract"]}).json()
    assert body["exit_code"] == 0
    assert "1 passed" in body["stdout"]


def test_tests_endpoint_missing_named_path_is_red(client: TestClient):
    """An empty or absent contract tree is a red fact, not a silent pass:
    pytest exits nonzero on a named path it cannot collect."""
    body = client.post("/tests", json={"paths": ["tests/contract"]}).json()
    assert body["exit_code"] != 0


def test_tests_endpoint_refuses_escaping_paths(client: TestClient):
    resp = client.post("/tests", json={"paths": ["../../etc"]})
    assert resp.status_code == 400


# --- /bundle -------------------------------------------------------------------


BUNDLE = {"branch": "feature/change", "message": "Add a feature", "author": "fd-impl-1"}


def bundled(body: dict, tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("bundle") / "change.bundle"
    path.write_bytes(base64.b64decode(body["bundle_b64"]))
    return path


def test_bundle_commits_the_workspace_as_it_stands(
    client: TestClient, workspace: Path, tmp_path_factory
):
    """Uncommitted work is the delivery: the platform makes one commit over
    the base ref, authored by the agent that asked, and leaves the working
    tree and its index alone."""
    (workspace / "feature.txt").write_text("new\n")
    (workspace / "hello.txt").write_text("hello, world\n")
    resp = client.post("/bundle", json=BUNDLE)
    assert resp.status_code == 200
    body = resp.json()
    base = (workspace / ".poseidon-base").read_text().strip()
    assert body["base_ref"] == base

    bundle_path = bundled(body, tmp_path_factory)
    git(workspace, "bundle", "verify", str(bundle_path))
    assert "refs/heads/feature/change" in git(
        workspace, "bundle", "list-heads", str(bundle_path)
    )
    assert git(workspace, "rev-parse", "feature/change^") == base
    assert git(workspace, "log", "-1", "--format=%an <%ae>|%s", "feature/change") == (
        "fd-impl-1 <fd-impl-1@agents.poseidon.invalid>|Add a feature"
    )
    changed = git(workspace, "diff", "--name-only", base, "feature/change")
    assert changed.splitlines() == ["feature.txt", "hello.txt"]
    # The workspace is where the agent left it.
    assert git(workspace, "rev-parse", "HEAD") == base
    assert "?? feature.txt" in git(workspace, "status", "--short")


def test_bundle_keeps_excluded_files_out_even_when_committed(
    client: TestClient, workspace: Path
):
    """The tree is staged from the base ref, not from the agent's index: a
    force-added `.poseidon-base` or bytecode file never reaches the
    delivery, whatever the agent committed."""
    (workspace / "pkg" / "__pycache__").mkdir()
    (workspace / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (workspace / "feature.txt").write_text("new\n")
    git(workspace, "add", "-f", ".poseidon-base", "pkg/__pycache__")
    git(workspace, "commit", "-m", "agent commit")
    assert client.post("/bundle", json=BUNDLE).status_code == 200

    base = (workspace / ".poseidon-base").read_text().strip()
    changed = git(workspace, "diff", "--name-only", base, "feature/change")
    assert changed.splitlines() == ["feature.txt"]


def test_bundle_points_a_second_branch_at_the_same_tree(
    client: TestClient, workspace: Path
):
    """Deny-and-recover on the branch name: the second call delivers the
    same tree under the name policy allows."""
    (workspace / "feature.txt").write_text("new\n")
    assert client.post("/bundle", json={**BUNDLE, "branch": "main"}).status_code == 200
    assert client.post("/bundle", json=BUNDLE).status_code == 200
    assert git(workspace, "rev-parse", "feature/change^{tree}") == git(
        workspace, "rev-parse", "main^{tree}"
    )


def test_bundle_carries_a_deletion(client: TestClient, workspace: Path):
    (workspace / "pkg" / "mod.py").unlink()
    (workspace / "feature.txt").write_text("new\n")
    assert client.post("/bundle", json=BUNDLE).status_code == 200
    base = (workspace / ".poseidon-base").read_text().strip()
    changed = git(workspace, "diff", "--name-status", base, "feature/change")
    assert changed.splitlines() == ["A\tfeature.txt", "D\tpkg/mod.py"]


def test_bundle_with_nothing_changed_400(client: TestClient, workspace: Path):
    """Only excluded files differ: nothing to deliver."""
    (workspace / "pkg" / "x.pyc").write_bytes(b"\0")
    resp = client.post("/bundle", json=BUNDLE)
    assert resp.status_code == 400
    assert "nothing to deliver" in resp.json()["detail"]


@pytest.mark.parametrize("branch", ["-x", "feature/a b", "feature/..x"])
def test_bundle_refuses_a_bad_branch_name(client: TestClient, workspace: Path, branch):
    (workspace / "feature.txt").write_text("new\n")
    resp = client.post("/bundle", json={**BUNDLE, "branch": branch})
    assert resp.status_code == 400


def test_bundle_missing_base_file_400(client: TestClient, workspace: Path):
    (workspace / "feature.txt").write_text("new\n")
    (workspace / ".poseidon-base").unlink()
    resp = client.post("/bundle", json=BUNDLE)
    assert resp.status_code == 400
    assert ".poseidon-base" in resp.json()["detail"]
