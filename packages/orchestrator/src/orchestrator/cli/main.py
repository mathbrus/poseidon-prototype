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
from orchestrator.cli.blueprint import app as blueprint_app
from orchestrator.config import TemporalSettings
from orchestrator.workflows.implement import (
    FAST_TIMEOUTS,
    ImplementInput,
    workflow_id,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(blueprint_app, name="blueprint")


async def _client(settings: TemporalSettings) -> Client:
    return await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )


@app.command()
def run(
    task_file: Path,
    run_id: str = typer.Option(None, help="Run id (generated when omitted)"),
    workflow: str = typer.Option(
        "implement",
        help="Which workflow to start: implement (a task for the engineer)"
        " or intake (a goal for the PM to scope, plan step 18)",
    ),
    detach: bool = typer.Option(False, help="Start the workflow and exit"),
    fast_timeouts: bool = typer.Option(
        False,
        "--fast-timeouts",
        help="Short per-activity bounds, sized for the mock brain "
        "(a wedged step then fails in seconds instead of minutes)",
    ),
) -> None:
    """Start a run for the task or goal described in TASK_FILE."""
    from orchestrator.workflows.intake import IntakeInput, intake_workflow_id

    settings = TemporalSettings()
    task_text = task_file.read_text()
    run_id = run_id or new_run_id()
    timeouts = FAST_TIMEOUTS if fast_timeouts else None
    if workflow == "intake":
        started_input: object = IntakeInput(
            run_id=run_id, goal_text=task_text, timeouts=timeouts
        )
        wf_id = intake_workflow_id(run_id)
    elif workflow == "implement":
        started_input = ImplementInput(
            run_id=run_id, task_text=task_text, timeouts=timeouts
        )
        wf_id = workflow_id(run_id)
    else:
        typer.echo(f"unknown workflow {workflow!r}", err=True)
        raise typer.Exit(2)

    async def _run() -> dict | None:
        client = await _client(settings)
        handle = await client.start_workflow(
            workflow,
            started_input,
            id=wf_id,
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
def review(
    pr_number: int,
    run_id: str = typer.Option(None, help="Run id (generated when omitted)"),
    detach: bool = typer.Option(
        False,
        help="Start the workflow and exit — the merge gate parks it until"
        " `poseidon signal <run_id> merge --workflow review` answers",
    ),
    fast_timeouts: bool = typer.Option(
        False,
        "--fast-timeouts",
        help="Short per-activity bounds, sized for the mock brain",
    ),
) -> None:
    """Start a review run for the pull request (§6.3, plan step 20)."""
    from orchestrator.workflows.review import ReviewInput, review_workflow_id

    settings = TemporalSettings()
    run_id = run_id or new_run_id()
    started_input = ReviewInput(
        run_id=run_id,
        pr_number=pr_number,
        timeouts=FAST_TIMEOUTS if fast_timeouts else None,
    )

    async def _run() -> dict | None:
        client = await _client(settings)
        handle = await client.start_workflow(
            "review",
            started_input,
            id=review_workflow_id(run_id),
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


def _workflow_id(workflow: str, run_id: str) -> str:
    """Workflow ids are `{workflow}-{run_id}` across all three workflows
    (§5.5) — the CLI only needs to know which family a run belongs to."""
    if workflow not in ("implement", "intake", "review"):
        typer.echo(f"unknown workflow {workflow!r}", err=True)
        raise typer.Exit(2)
    return f"{workflow}-{run_id}"


@app.command()
def signal(
    run_id: str,
    name: str,
    approved: bool = typer.Option(True, help="Gate verdict carried by the signal"),
    payload: str = typer.Option(None, help="Optional payload"),
    workflow: str = typer.Option(
        "implement", help="Which workflow family the run belongs to:"
        " implement, intake or review"
    ),
) -> None:
    """Send a gate signal to a run's workflow — the HITL verdict path
    (e.g. `poseidon signal <run_id> merge --workflow review`)."""
    settings = TemporalSettings()
    wf_id = _workflow_id(workflow, run_id)

    async def _signal() -> None:
        client = await _client(settings)
        handle = client.get_workflow_handle(wf_id)
        await handle.signal(name, args=[approved, payload])

    asyncio.run(_signal())
    typer.echo(f"signalled {wf_id}: {name}")


@app.command()
def status(
    run_id: str,
    workflow: str = typer.Option(
        "implement", help="Which workflow family the run belongs to:"
        " implement, intake or review"
    ),
) -> None:
    """Show the workflow status (and result, when completed) of a run."""
    settings = TemporalSettings()
    wf_id = _workflow_id(workflow, run_id)

    async def _status() -> dict:
        client = await _client(settings)
        handle = client.get_workflow_handle(wf_id)
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
