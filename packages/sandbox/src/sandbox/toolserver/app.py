"""Sandbox tool server.

Runs inside the sandbox container and listens for the gateway only:

    POST /files/read  {path, view_range?}                       -> {content, start_line, total_lines}
    POST /files/write {path, content}                           -> {ok}
    POST /files/edit  {path, command, old_str?, new_str, insert_line?} -> {ok, snippet}
    POST /files/list  {path}                                    -> {files, omitted}
    POST /exec        {cmd, timeout_s}  -> {exit_code, stdout, stderr}
    POST /tests       {paths?}          -> {exit_code, stdout, stderr}
    POST /bundle      {branch, message, author} -> {bundle_b64, base_ref}

`/files/edit` is the partial edit: `str_replace` swaps text that occurs
exactly once, `insert` adds lines after a line number. Both answer with a
numbered snippet around the change. `/files/read` takes an optional
`[start, end]` line range (1-based, inclusive, `-1` for end of file);
without one it returns the first `READ_LINE_LIMIT` lines, and
`total_lines` says how many there are.

`/files/list` is the tree under a directory as git sees it — tracked and
untracked files, ignored ones left out — as workspace-relative paths, at
most `LIST_LIMIT` of them; `omitted` counts the rest.

`/tests` with `paths` appends them to the test command — the platform's
scoped contract-verify run. Naming the paths explicitly means a pytest
config that ignores them cannot silently green the run: collecting
nothing from a named path is a nonzero exit, not a pass.

`/bundle` is the delivery commit, made here rather than by whoever holds a
shell: one commit over the base ref whose tree is the workspace as it
stands, staged from a fresh index seeded from the base ref, so what
`.git/info/exclude` keeps out (`.poseidon-base`, bytecode) stays out even
if something force-added it. The branch is pointed at that commit, the
working tree is left as it is, and a second call on another branch name
points that one at the same tree. A workspace that matches the base ref
has nothing to deliver: HTTP 400.

All paths are resolved inside the workspace (default /workspace, override with
WORKSPACE_DIR); anything escaping it is HTTP 400. The bundle base ref is read
from `.poseidon-base`, written at provision time by the sandbox backend.
"""

import base64
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from common.telemetry import get_logger
from common.text import numbered_lines

BASE_REF_FILE = ".poseidon-base"
TESTS_TIMEOUT_S = 600
# Same convention as coreutils `timeout`.
TIMEOUT_EXIT_CODE = 124
# Lines of context on each side of an edit in the snippet returned.
SNIPPET_CONTEXT = 4
GIT_TIMEOUT_S = 60
# The most lines a read without a range returns; a longer file is read on
# in ranges.
READ_LINE_LIMIT = 2000
# The most paths a listing returns.
LIST_LIMIT = 500
# The delivery commit's author is the agent that asked for it; git wants an
# address, and this domain says it is no mailbox.
AUTHOR_EMAIL_DOMAIN = "agents.poseidon.invalid"
# Where the delivery commit is staged: inside `.git`, so never in the tree,
# and apart from the workspace's own index, which stays as the agent left it.
DELIVERY_INDEX = "poseidon-delivery-index"


class ToolserverSettings(BaseSettings):
    """Set on the sandbox Job by the backend's manifest (`sandbox.manifest`)."""

    workspace_dir: Path = Path("/workspace")
    test_cmd: str = "pytest -q"


class ReadRequest(BaseModel):
    path: str
    view_range: tuple[int, int] | None = None


class WriteRequest(BaseModel):
    path: str
    content: str


class EditRequest(BaseModel):
    path: str
    command: Literal["str_replace", "insert"]
    old_str: str | None = None
    new_str: str
    insert_line: int | None = None


class ListRequest(BaseModel):
    path: str


class ExecRequest(BaseModel):
    cmd: str
    timeout_s: float = 60


class BundleRequest(BaseModel):
    branch: str
    message: str
    author: str


class TestsRequest(BaseModel):
    paths: list[str] | None = None


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data


def _snippet(lines: list[str], start: int, end: int) -> str:
    """Numbered lines around [start, end) (0-based line indexes)."""
    lo = max(0, start - SNIPPET_CONTEXT)
    hi = min(len(lines), end + SNIPPET_CONTEXT)
    return numbered_lines(lines[lo:hi], lo + 1)


def create_app(workspace: Path | None = None) -> FastAPI:
    settings = ToolserverSettings()
    ws = (workspace or settings.workspace_dir).resolve()
    log = get_logger("sandbox.toolserver")
    app = FastAPI(title="poseidon-sandbox-toolserver")

    def resolve(path: str) -> Path:
        candidate = (ws / path).resolve()
        if not candidate.is_relative_to(ws):
            raise HTTPException(status_code=400, detail=f"path escapes workspace: {path}")
        return candidate

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        """One git command in the workspace; a failure is the caller's 400."""
        proc = subprocess.run(
            ["git", *args],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=env,
        )
        if proc.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"git {args[0]} failed: {proc.stderr.strip()}",
            )
        return proc.stdout.strip()

    def run_shell(cmd: str, timeout_s: float) -> dict[str, Any]:
        log.info("exec", extra={"cmd": cmd, "timeout_s": timeout_s})
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=ws, timeout=timeout_s, capture_output=True, text=True
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": TIMEOUT_EXIT_CODE,
                "stdout": _as_text(exc.stdout),
                "stderr": _as_text(exc.stderr)
                + f"\ncommand timed out after {timeout_s}s",
            }
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.post("/files/read")
    def files_read(req: ReadRequest) -> dict[str, Any]:
        target = resolve(req.path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {req.path}")
        text = target.read_text()
        lines = text.splitlines(keepends=True)
        if req.view_range is None:
            return {
                "content": "".join(lines[:READ_LINE_LIMIT]),
                "start_line": 1,
                "total_lines": len(lines),
            }
        start, end = req.view_range
        if end == -1:
            end = len(lines)
        if start < 1 or start > len(lines) or end < start:
            raise HTTPException(
                status_code=400,
                detail=f"view_range {list(req.view_range)} outside 1..{len(lines)}",
            )
        return {
            "content": "".join(lines[start - 1 : end]),
            "start_line": start,
            "total_lines": len(lines),
        }

    @app.post("/files/write")
    def files_write(req: WriteRequest) -> dict[str, Any]:
        target = resolve(req.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(req.content)
        return {"ok": True}

    @app.post("/files/edit")
    def files_edit(req: EditRequest) -> dict[str, Any]:
        target = resolve(req.path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {req.path}")
        text = target.read_text()
        if req.command == "str_replace":
            if req.old_str is None:
                raise HTTPException(status_code=400, detail="str_replace needs old_str")
            head, found, tail = text.partition(req.old_str)
            if not found:
                raise HTTPException(
                    status_code=400, detail=f"old_str not found in {req.path}"
                )
            if req.old_str in tail:
                hits = 1 + tail.count(req.old_str)
                raise HTTPException(
                    status_code=400,
                    detail=f"old_str occurs {hits} times in {req.path}; include "
                    "more context to make it unique",
                )
            new_text = head + req.new_str + tail
            first = head.count("\n")
            last = first + req.new_str.count("\n") + 1
        else:
            if req.insert_line is None:
                raise HTTPException(status_code=400, detail="insert needs insert_line")
            lines = text.splitlines(keepends=True)
            if req.insert_line < 0 or req.insert_line > len(lines):
                raise HTTPException(
                    status_code=400,
                    detail=f"insert_line {req.insert_line} outside 0..{len(lines)}",
                )
            block = req.new_str if req.new_str.endswith("\n") else req.new_str + "\n"
            if req.insert_line and not lines[req.insert_line - 1].endswith("\n"):
                lines[req.insert_line - 1] += "\n"
            lines[req.insert_line : req.insert_line] = [block]
            new_text = "".join(lines)
            first = req.insert_line
            last = first + block.count("\n")
        target.write_text(new_text)
        return {
            "ok": True,
            "snippet": _snippet(new_text.splitlines(keepends=True), first, last),
        }

    @app.post("/files/list")
    def files_list(req: ListRequest) -> dict[str, Any]:
        target = resolve(req.path)
        if not target.is_dir():
            raise HTTPException(status_code=404, detail=f"no such directory: {req.path}")
        listed = git(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            str(target.relative_to(ws)),
        )
        # A tracked file the agent deleted is still in the index; the
        # listing is the tree as it stands.
        files = sorted({p for p in listed.split("\0") if p and (ws / p).is_file()})
        return {"files": files[:LIST_LIMIT], "omitted": max(0, len(files) - LIST_LIMIT)}

    @app.post("/exec")
    def exec_cmd(req: ExecRequest) -> dict[str, Any]:
        return run_shell(req.cmd, req.timeout_s)

    @app.post("/tests")
    def run_tests(req: TestsRequest | None = None) -> dict[str, Any]:
        cmd = settings.test_cmd
        paths = (req.paths if req else None) or []
        for path in paths:
            resolve(path)  # workspace containment, like every path input
        if paths:
            cmd = " ".join([cmd, *(shlex.quote(p) for p in sorted(paths))])
        return run_shell(cmd, TESTS_TIMEOUT_S)

    @app.post("/bundle")
    def bundle(req: BundleRequest) -> dict[str, Any]:
        base_file = ws / BASE_REF_FILE
        if not base_file.is_file():
            raise HTTPException(
                status_code=400, detail=f"base ref not recorded ({BASE_REF_FILE} missing)"
            )
        base_ref = base_file.read_text().strip()
        if req.branch.startswith("-"):
            raise HTTPException(
                status_code=400, detail=f"{req.branch!r} is not a valid branch name"
            )
        git("check-ref-format", "--branch", req.branch)
        log.info("bundle", extra={"branch": req.branch, "base_ref": base_ref})
        index = ws / ".git" / DELIVERY_INDEX
        staged = {**os.environ, "GIT_INDEX_FILE": str(index)}
        try:
            git("read-tree", base_ref, env=staged)
            git("add", "-A", env=staged)
            tree = git("write-tree", env=staged)
        finally:
            index.unlink(missing_ok=True)
        if tree == git("rev-parse", f"{base_ref}^{{tree}}"):
            raise HTTPException(
                status_code=400,
                detail="nothing to deliver: the workspace matches the base ref",
            )
        email = f"{req.author}@{AUTHOR_EMAIL_DOMAIN}"
        identity = {
            **os.environ,
            "GIT_AUTHOR_NAME": req.author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": req.author,
            "GIT_COMMITTER_EMAIL": email,
        }
        commit = git("commit-tree", tree, "-p", base_ref, "-m", req.message, env=identity)
        git("update-ref", f"refs/heads/{req.branch}", commit)
        proc = subprocess.run(
            ["git", "bundle", "create", "-", f"{base_ref}..{req.branch}"],
            cwd=ws,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
        )
        if proc.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"git bundle failed: {_as_text(proc.stderr).strip()}",
            )
        return {
            "bundle_b64": base64.b64encode(proc.stdout).decode(),
            "base_ref": base_ref,
        }

    return app


app = create_app()
