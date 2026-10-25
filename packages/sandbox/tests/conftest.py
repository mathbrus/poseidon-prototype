import os
import subprocess
from pathlib import Path

import pytest

from sandbox.k8s_jobs import _PREPARE_CMD

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",  # keep user gitconfig out of the tests
    "PATH": os.environ.get("PATH", os.defpath),
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


def cloned(path: Path) -> Path:
    """A workspace as the backend unpacks it: a clone of the target
    repository — one base commit, and the remotes a clone carries."""
    git(path, "init", "-b", "main")
    (path / "hello.txt").write_text("hello\n")
    (path / "pkg").mkdir()
    (path / "pkg" / "mod.py").write_text("x = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-m", "base")
    git(path, "remote", "add", "origin", "https://github.com/example-org/repo.git")
    git(path, "remote", "add", "upstream", "https://github.com/example-org/fork.git")
    return path


def prepare(path: Path) -> Path:
    """Run the backend's own preparation command on `path`, as it runs it
    in the sandbox after unpacking."""
    subprocess.run(
        ["sh", "-c", _PREPARE_CMD.format(workspace=path)],
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A provisioned workspace: the clone, prepared."""
    return prepare(cloned(tmp_path))
