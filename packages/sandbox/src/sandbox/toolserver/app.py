"""Sandbox tool server.

Runs inside the sandbox container and listens for the gateway only:

    POST /files/read  {path, view_range?}                       -> {content, start_line, total_lines}
    POST /files/write {path, content}                           -> {ok}
    POST /files/edit  {path, command, old_str?, new_str, insert_line?} -> {ok, snippet}
    POST /files/list  {path}                                    -> {entries: [{name, type}]}
    POST /exec        {cmd, timeout_s}  -> {exit_code, stdout, stderr}
    POST /tests       {paths?}          -> {exit_code, stdout, stderr}
    POST /bundle      {branch}          -> {bundle_b64, base_ref}

`/files/edit` is the partial edit: `str_replace` swaps text that occurs
exactly once, `insert` adds lines after a line number. Both answer with a
numbered snippet around the change. `/files/read` takes an optional
`[start, end]` line range (1-based, inclusive, `-1` for end of file).

`/tests` with `paths` appends them to the test command — the platform's
scoped contract-verify run. Naming the paths explicitly means a pytest
config that ignores them cannot silently green the run: collecting
nothing from a named path is a nonzero exit, not a pass.

All paths are resolved inside the workspace (default /workspace, override with
WORKSPACE_DIR); anything escaping it is HTTP 400. The bundle base ref is read
from `.poseidon-base`, written at provision time by the sandbox backend.
"""

import base64
import shlex
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from common.telemetry import get_logger

BASE_REF_FILE = ".poseidon-base"
TESTS_TIMEOUT_S = 600
# Same convention as coreutils `timeout`.
TIMEOUT_EXIT_CODE = 124
# Lines of context on each side of an edit in the snippet returned.
SNIPPET_CONTEXT = 4


class ToolserverSettings(BaseSettings):
    """Set on the sandbox Job by the backend's manifest (`sandbox.manifest`)."""

    workspace_dir: Path = Path("/workspace")
    test_cmd: str = "pytest -q"


class ReadRequest(BaseModel):
    path: str
    view_range: list[int] | None = None


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


class TestsRequest(BaseModel):
    paths: list[str] | None = None


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data


def _numbered(lines: list[str], first: int) -> str:
    return "".join(f"{first + i:6d}\t{line}" for i, line in enumerate(lines))


def _snippet(lines: list[str], start: int, end: int) -> str:
    """Numbered lines around [start, end) (0-based line indexes)."""
    lo = max(0, start - SNIPPET_CONTEXT)
    hi = min(len(lines), end + SNIPPET_CONTEXT)
    return _numbered(lines[lo:hi], lo + 1)


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
        if req.view_range is None:
            return {"content": text, "start_line": 1, "total_lines": text.count("\n")}
        lines = text.splitlines(keepends=True)
        if len(req.view_range) != 2:
            raise HTTPException(status_code=400, detail="view_range must be [start, end]")
        start, end = req.view_range
        if end == -1:
            end = len(lines)
        if start < 1 or start > max(len(lines), 1) or end < start:
            raise HTTPException(
                status_code=400,
                detail=f"view_range {req.view_range} outside 1..{len(lines)}",
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
            hits = text.count(req.old_str)
            if hits == 0:
                raise HTTPException(
                    status_code=400, detail=f"old_str not found in {req.path}"
                )
            if hits > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"old_str occurs {hits} times in {req.path}; include "
                    "more context to make it unique",
                )
            at = text.index(req.old_str)
            new_text = text[:at] + req.new_str + text[at + len(req.old_str) :]
            first = text[:at].count("\n")
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
            if (
                req.insert_line
                and lines
                and not lines[req.insert_line - 1].endswith("\n")
            ):
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
        entries = [
            {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
            for entry in sorted(target.iterdir(), key=lambda e: e.name)
        ]
        return {"entries": entries}

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
        log.info("bundle", extra={"branch": req.branch, "base_ref": base_ref})
        proc = subprocess.run(
            ["git", "bundle", "create", "-", f"{base_ref}..{req.branch}"],
            cwd=ws,
            capture_output=True,
            timeout=60,
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
