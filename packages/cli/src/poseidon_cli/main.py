"""`poseidon` — the console's command line.

Every command but `login` is one console call with the human's token:
the CLI holds no cluster credentials, reaches no workflow engine and no
database, and performs no act of its own. What it prints is what the
console answered. `login` mints the token, once, with the kubeconfig the
caller already holds.
"""

import json
import sys
from pathlib import Path
from typing import Any

import typer

from common.topology import CONSOLE_TOKEN_AUDIENCE, human_service_account
from poseidon_cli.client import TOKEN_VAR, URL_VAR, ConsoleClient, ConsoleError

app = typer.Typer(no_args_is_help=True, add_completion=False)
blueprint_app = typer.Typer(
    no_args_is_help=True, help="Preview and apply company blueprints."
)
heartbeat_app = typer.Typer(no_args_is_help=True, help="The company's standing loop.")
app.add_typer(blueprint_app, name="blueprint")
app.add_typer(heartbeat_app, name="heartbeat")

DEFAULT_PLATFORM_NAMESPACE = "poseidon-platform"


def _client() -> ConsoleClient:
    client = ConsoleClient.from_env()
    if not client.token:
        typer.echo(
            f"no token: set {TOKEN_VAR} (`export {TOKEN_VAR}=$(poseidon login <name>)`)"
            f" and {URL_VAR} if the console is not at {client.base_url}",
            err=True,
        )
        raise typer.Exit(2)
    return client


def _show(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2))


def _call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except ConsoleError as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        rule = getattr(exc, "rule", None)
        typer.echo(f"{detail}" + (f" (rule: {rule})" if rule else ""), err=True)
        raise typer.Exit(1) from exc


@app.command()
def login(
    name: str,
    namespace: str = typer.Option(
        DEFAULT_PLATFORM_NAMESPACE, help="The platform namespace the chart installed into"
    ),
    duration: int = typer.Option(3600, help="Token lifetime, seconds"),
) -> None:
    """Mint a token for a human the chart declared, with your own cluster
    credentials, and print it: `export POSEIDON_TOKEN=$(poseidon login owner)`.
    The token is for the console's audience only — worthless anywhere else."""
    from kubernetes import client as k8s
    from kubernetes import config as k8s_config

    k8s_config.load_kube_config()
    request = k8s.AuthenticationV1TokenRequest(
        spec=k8s.V1TokenRequestSpec(
            audiences=[CONSOLE_TOKEN_AUDIENCE], expiration_seconds=duration
        )
    )
    try:
        token = k8s.CoreV1Api().create_namespaced_service_account_token(
            human_service_account(name), namespace, request
        )
    except k8s.ApiException as exc:
        typer.echo(
            f"could not mint a token for {human_service_account(name)!r} in"
            f" {namespace!r}: {exc.reason}",
            err=True,
        )
        raise typer.Exit(1) from exc
    typer.echo(token.status.token)


@app.command()
def whoami() -> None:
    """Who the console takes you for, and which company it hosts."""
    _show(_call(_client().whoami))


@blueprint_app.command()
def preview(blueprint: Path) -> None:
    """Show what applying BLUEPRINT would change, and what it would cost."""
    result = _call(_client().preview, blueprint.read_text())
    typer.echo(result["diff"])


@blueprint_app.command()
def apply(
    blueprint: Path,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the diff confirmation"),
) -> None:
    """Apply BLUEPRINT: the console compiles it against the live registry
    and submits the CRs; the operator converges them."""
    client = _client()
    text = blueprint.read_text()
    diff = _call(client.preview, text)["diff"]
    typer.echo(diff, err=True)
    if not yes and "no changes" not in diff:
        typer.confirm("apply?", abort=True)
    result = _call(client.apply, text)
    typer.echo(
        f"applied {result['company']}: {result['created']} created,"
        f" {result['updated']} updated ({result['rule']})"
    )


@app.command()
def run(
    issue_number: int,
    detach: bool = typer.Option(False, help="Start the run and exit"),
) -> None:
    """Start one implement run by hand on a ticket the company filed — an
    operator's repro. The heartbeat starts the company's runs on its own."""
    _start("implement", issue_number, detach)


@app.command()
def review(
    pr_number: int,
    detach: bool = typer.Option(
        False,
        help="Start the run and exit — the merge gate parks it until"
        " `poseidon signal <run_id> merge` answers",
    ),
) -> None:
    """Start one review run by hand on a delivered pull request — an
    operator's repro, like `run`."""
    _start("review", pr_number, detach)


def _start(workflow: str, number: int, detach: bool) -> None:
    client = _client()
    started = _call(client.start_run, workflow, number)
    typer.echo(f"started {started['workflow_id']} (run_id {started['run_id']})", err=True)
    if detach:
        return
    import time

    while True:
        record = _call(client.run, started["run_id"])
        if record["status"] not in ("running",) or record["execution"]["status"] not in (
            "RUNNING",
            "NOT_FOUND",
        ):
            break
        time.sleep(5)
    _show(record)
    if record.get("status") != "done":
        sys.exit(1)


@app.command()
def signal(
    run_id: str,
    gate: str,
    approved: bool = typer.Option(True, help="The verdict: --approved or --no-approved"),
    payload: str = typer.Option(None, help="A note recorded with the verdict"),
) -> None:
    """Answer a gate a run parked on — `merge` or `budget`. The console
    records the verdict under your name, then delivers it."""
    _show(_call(_client().resolve, run_id, gate, approved, payload))


@app.command()
def cancel(run_id: str, reason: str = typer.Option("", help="Why")) -> None:
    """Cancel a run; its teardown compensates."""
    _show(_call(_client().cancel, run_id, reason))


@app.command()
def status(run_id: str) -> None:
    """The run's record and its workflow's execution."""
    _show(_call(_client().run, run_id))


@app.command()
def story(run_id: str) -> None:
    """The run's audit story — every decision, every source, in order."""
    _show(_call(_client().story, run_id))


@app.command()
def spend(run_id: str) -> None:
    """The run's attributed spend."""
    _show(_call(_client().spend, run_id))


@heartbeat_app.command()
def state(
    since: str = typer.Option(None, help="Ticks at or after this timestamp"),
) -> None:
    """The standing loop's execution and its ticks."""
    _show(_call(_client().heartbeat, since))


@heartbeat_app.command()
def wake() -> None:
    """Nudge the loop to look now — never an instruction."""
    _show(_call(_client().wake))


if __name__ == "__main__":
    app()
