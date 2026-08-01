"""File-based run manifests — written from activities onto the worker's
runs volume. Transitional: the registry `runs` table replaces this at plan
step 8. `audit_lines` from the PoC manifest is gone — the audit JSONL lives
on the gateway's PVC now and is queried there (tests do so via kubectl)."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class Manifest(BaseModel):
    run_id: str
    task: str
    status: Literal["running", "done", "failed"]
    created_at: str
    finished_at: str | None = None
    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / run_id / "manifest.json"


def write_manifest(runs_dir: Path, manifest: Manifest) -> None:
    path = manifest_path(runs_dir, manifest.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n")


def read_manifest(runs_dir: Path, run_id: str) -> Manifest:
    return Manifest.model_validate_json(manifest_path(runs_dir, run_id).read_text())
