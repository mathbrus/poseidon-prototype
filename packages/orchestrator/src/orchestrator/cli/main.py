"""`poseidon blueprint|signal|status|run|review` — a thin Temporal client.

Reaches the Temporal frontend via TEMPORAL_ADDRESS (default 127.0.0.1:7233,
e.g. `kubectl -n poseidon-platform port-forward svc/temporal-frontend
7233:7233`). Holds no credentials: runs are executed by the in-cluster
worker; the CLI only starts, signals and inspects workflows.

The company starts its own runs from the heartbeat. `run` and `review`
start one by hand — an operator reproducing a run, never the loop.
"""

import asyncio
import json
import sys

import typer
from temporalio.client import Client, WorkflowExecutionStatus

from common.ids import new_run_id
from common.workflows import TASK_QUEUE, TEMPORAL_NAMESPACE, WORKFLOWS, workflow_id
from orchestrator.cli.blueprint import app as blueprint_app
from orchestrator.config import TemporalSettings
from orchestrator.workflows import ImplementInput, ReviewInput

app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(blueprint_app, name="blueprint")


async def _client(settings: TemporalSettings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=TEMPORAL_NAMESPACE)


@app.command()
def run(
    issue_number: int,
    run_id: str = typer.Option(None, help="Run id (generated when omitted)"),
    detach: bool = typer.Option(False, help="Start the workflow and exit"),
) -> None:
    """Start one implement run by hand on a ticket the company filed — an
    operator's repro. The heartbeat starts the company's runs on its own."""
    run_id = run_id or new_run_id()
    _start(
        "implement",
        run_id,
        ImplementInput(run_id=run_id, issue_number=issue_number),
        detach,
    )


@app.command()
def review(
    pr_number: int,
    run_id: str = typer.Option(None, help="Run id (generated when omitted)"),
    detach: bool = typer.Option(
        False,
        help="Start the workflow and exit — the merge gate parks it until"
        " `poseidon signal <run_id> merge --workflow review` answers",
    ),
) -> None:
    """Start one review run by hand on a delivered pull request — an
    operator's repro, like `run`."""
    run_id = run_id or new_run_id()
    started_input = ReviewInput(run_id=run_id, pr_number=pr_number)
    _start("review", run_id, started_input, detach)


def _start(workflow: str, run_id: str, started_input: object, detach: bool) -> None:
    """Start the workflow; unless detached, wait for the run record, print
    it, and exit non-zero when the run did not finish `done`."""
    settings = TemporalSettings()

    async def _run() -> dict | None:
        client = await _client(settings)
        handle = await client.start_workflow(
            workflow,
            started_input,
            id=workflow_id(workflow, run_id),
            task_queue=TASK_QUEUE,
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
    """The workflow id a `--workflow` name and a run id address; for the
    heartbeat the run id argument is the company name."""
    if workflow not in WORKFLOWS:
        typer.echo(f"unknown workflow {workflow!r}", err=True)
        raise typer.Exit(2)
    return workflow_id(workflow, run_id)


@app.command()
def signal(
    run_id: str,
    name: str,
    approved: bool = typer.Option(True, help="Gate verdict carried by the signal"),
    payload: str = typer.Option(None, help="Optional payload"),
    workflow: str = typer.Option(
        "implement",
        help=f"Which workflow the run belongs to: {', '.join(WORKFLOWS)}",
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
        "implement",
        help=f"Which workflow the run belongs to: {', '.join(WORKFLOWS)}"
        " (for heartbeat, RUN_ID is the company name)",
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
