"""The implement flow (§4.1): create_run -> fetch_workspace ->
provision_sandbox -> dispatch_task -> teardown.

Deliberately not Temporal, but each step is named after the future Temporal
activity and kept pure enough to migrate. Timeout, iteration and failure
handling live here, not in the agent; the sandbox is destroyed no matter how
the run ends, and the manifest under runs/<run_id>/manifest.json is always
finalized.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from common.ids import new_run_id
from common.telemetry import get_logger
from runner.config import RunnerSettings
from sandbox.contract import SandboxBackend, SandboxHandle


class GatewayApi(Protocol):
    async def fetch_workspace(self, repo: str, ref: str) -> bytes: ...

    async def register_sandbox(self, run_id: str, url: str) -> None: ...


class AgentApi(Protocol):
    async def dispatch(self, run_id: str, task_text: str) -> None: ...

    async def status(self, run_id: str) -> dict[str, Any]: ...


class Manifest(BaseModel):
    run_id: str
    task: str
    status: Literal["running", "done", "failed"]
    created_at: str
    finished_at: str | None = None
    pr_url: str | None = None
    iterations: int = 0
    audit_lines: int = 0
    error: str | None = None


class StepFailed(Exception):
    def __init__(self, step: str, message: str) -> None:
        self.step = step
        super().__init__(f"{step}: {message}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(settings: RunnerSettings, run_id: str) -> Path:
    return settings.runs_dir / run_id / "manifest.json"


def write_manifest(settings: RunnerSettings, manifest: Manifest) -> None:
    path = manifest_path(settings, manifest.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n")


def read_manifest(settings: RunnerSettings, run_id: str) -> Manifest:
    return Manifest.model_validate_json(manifest_path(settings, run_id).read_text())


def count_audit_lines(settings: RunnerSettings, run_id: str) -> int:
    """Number of audit events this run produced (C1 JSONL, one per call)."""
    if not settings.audit_path.is_file():
        return 0
    count = 0
    for line in settings.audit_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("run_id") == run_id:
            count += 1
    return count


async def _step(name: str, coro: Any, timeout_s: float) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout_s)
    except TimeoutError:
        raise StepFailed(name, f"timed out after {timeout_s}s") from None
    except StepFailed:
        raise
    except Exception as exc:
        raise StepFailed(name, str(exc)) from exc


async def _await_agent(
    agent: AgentApi, run_id: str, task_text: str, settings: RunnerSettings
) -> dict[str, Any]:
    await agent.dispatch(run_id, task_text)
    while True:
        status = await agent.status(run_id)
        if status["status"] != "running":
            return status
        await asyncio.sleep(settings.poll_interval_s)


async def execute_run(
    settings: RunnerSettings,
    task_text: str,
    backend: SandboxBackend,
    gateway: GatewayApi,
    agent: AgentApi,
    run_id: str | None = None,
) -> Manifest:
    # -- create_run ---------------------------------------------------------
    run_id = run_id or new_run_id()
    log = get_logger("runner", run_id=run_id)
    manifest = Manifest(
        run_id=run_id, task=task_text, status="running", created_at=_now()
    )
    write_manifest(settings, manifest)
    log.info("run created")

    handle: SandboxHandle | None = None
    error: str | None = None
    try:
        # -- fetch_workspace --------------------------------------------------
        workspace_tar = await _step(
            "fetch_workspace",
            gateway.fetch_workspace(settings.target_repo, settings.base_ref),
            settings.step_timeout_s,
        )
        log.info("workspace fetched", extra={"bytes": len(workspace_tar)})

        # -- provision_sandbox ------------------------------------------------
        handle = await _step(
            "provision_sandbox",
            asyncio.to_thread(backend.provision, run_id, workspace_tar),
            settings.step_timeout_s,
        )
        await _step(
            "provision_sandbox",
            gateway.register_sandbox(run_id, handle.url),
            settings.step_timeout_s,
        )
        log.info("sandbox ready", extra={"url": handle.url})

        # -- dispatch_task ----------------------------------------------------
        status = await _step(
            "dispatch_task",
            _await_agent(agent, run_id, task_text, settings),
            settings.agent_timeout_s,
        )
        manifest.pr_url = status.get("pr_url")
        manifest.iterations = status.get("iterations", 0)
        error = status.get("error")
        log.info("agent finished", extra={"agent_status": status})
    except StepFailed as exc:
        error = str(exc)
        log.info("step failed", extra={"step": exc.step, "error": str(exc)})
    finally:
        # -- teardown ----------------------------------------------------------
        if handle is not None:
            try:
                await asyncio.to_thread(backend.destroy, handle)
            except Exception as exc:
                error = error or f"teardown: {exc}"
                log.info("teardown failed", extra={"error": str(exc)})
        manifest.status = "failed" if error else "done"
        manifest.error = error
        manifest.finished_at = _now()
        manifest.audit_lines = count_audit_lines(settings, run_id)
        write_manifest(settings, manifest)
        log.info("run finalized", extra={"status": manifest.status})
    return manifest
