"""Sandbox tool server — contract C4.

Runs inside the sandbox container and listens for the gateway only:

    POST /files/read  {path}            -> {content}
    POST /files/write {path, content}   -> {ok}
    POST /files/list  {path}            -> {entries: [{name, type}]}
    POST /exec        {cmd, timeout_s}  -> {exit_code, stdout, stderr}
    POST /tests       {}                -> {exit_code, stdout, stderr}
    POST /bundle      {branch}          -> {bundle_b64, base_ref}

All paths are resolved inside the workspace (default /workspace, override with
WORKSPACE_DIR); anything escaping it is HTTP 400. The bundle base ref is read
from `.poseidon-base`, written at provision time by the sandbox backend.
"""

import base64
import os
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.telemetry import get_logger

DEFAULT_WORKSPACE = Path("/workspace")
BASE_REF_FILE = ".poseidon-base"
DEFAULT_TEST_CMD = "pytest -q"
TESTS_TIMEOUT_S = 600
# Same convention as coreutils `timeout`.
TIMEOUT_EXIT_CODE = 124


class ReadRequest(BaseModel):
    path: str


class WriteRequest(BaseModel):
    path: str
    content: str


class ListRequest(BaseModel):
    path: str


class ExecRequest(BaseModel):
    cmd: str
    timeout_s: float = 60


class BundleRequest(BaseModel):
    branch: str


def _as_text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data


def create_app(workspace: Path | None = None) -> FastAPI:
    ws = (
        workspace or Path(os.environ.get("WORKSPACE_DIR", str(DEFAULT_WORKSPACE)))
    ).resolve()
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
        return {"content": target.read_text()}

    @app.post("/files/write")
    def files_write(req: WriteRequest) -> dict[str, Any]:
        target = resolve(req.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(req.content)
        return {"ok": True}

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
    def run_tests() -> dict[str, Any]:
        return run_shell(os.environ.get("TEST_CMD", DEFAULT_TEST_CMD), TESTS_TIMEOUT_S)

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
