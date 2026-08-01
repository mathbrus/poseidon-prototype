"""`poseidon run|signal|status` — a thin Temporal client (§4.2), successor
of the deleted `poseidon-poc` entry point.

Reaches the Temporal frontend via TEMPORAL_ADDRESS (default 127.0.0.1:7233,
e.g. `kubectl -n poseidon-platform port-forward svc/temporal-frontend
7233:7233`). Holds no credentials: runs are executed by the in-cluster
worker; the CLI only starts, signals and inspects workflows.
"""

import asyncio
import json
import sys
from pathlib import Path

import typer
from temporalio.client import Client, WorkflowExecutionStatus

from common.ids import new_run_id
from orchestrator.config import TemporalSettings
from orchestrator.workflows.implement import ImplementInput, workflow_id

app = typer.Typer(no_args_is_help=True, add_completion=False)


async def _client(settings: TemporalSettings) -> Client:
    return await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )


@app.command()
def run(
    task_file: Path,
    run_id: str = typer.Option(None, help="Run id (generated when omitted)"),
    detach: bool = typer.Option(False, help="Start the workflow and exit"),
) -> None:
    """Start an implement run for the task described in TASK_FILE."""
    settings = TemporalSettings()
    task_text = task_file.read_text()
    run_id = run_id or new_run_id()

    async def _run() -> dict | None:
        client = await _client(settings)
        handle = await client.start_workflow(
            "implement",
            ImplementInput(run_id=run_id, task_text=task_text),
            id=workflow_id(run_id),
            task_queue=settings.task_queue,
        )
        typer.echo(f"started {handle.id} (run_id {run_id})", err=True)
        if detach:
            return None
        return await handle.result()

    record = asyncio.run(_run())
    if record is not None:
        typer.echo(json.dumps(record, indent=2))
        if record.get("status") != "done":
            sys.exit(1)


@app.command()
def signal(
    run_id: str,
    name: str,
    approved: bool = typer.Option(True, help="Gate verdict carried by the signal"),
    payload: str = typer.Option(None, help="Optional payload"),
) -> None:
    """Send a gate signal to a run's workflow (no gate consumes these at M1)."""
    settings = TemporalSettings()

    async def _signal() -> None:
        client = await _client(settings)
        handle = client.get_workflow_handle(workflow_id(run_id))
        await handle.signal(name, args=[approved, payload])

    asyncio.run(_signal())
    typer.echo(f"signalled {workflow_id(run_id)}: {name}")


@app.command()
def status(run_id: str) -> None:
    """Show the workflow status (and result, when completed) of a run."""
    settings = TemporalSettings()

    async def _status() -> dict:
        client = await _client(settings)
        handle = client.get_workflow_handle(workflow_id(run_id))
        desc = await handle.describe()
        out: dict = {
            "workflow_id": handle.id,
            "status": desc.status.name if desc.status else "UNKNOWN",
        }
        if desc.status == WorkflowExecutionStatus.COMPLETED:
            out["record"] = await handle.result()
        return out

    typer.echo(json.dumps(asyncio.run(_status()), indent=2))


if __name__ == "__main__":
    app()
