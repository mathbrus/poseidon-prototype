"""Runner CLI (§4.1): `poseidon-poc run <task-file>` and
`poseidon-poc status <run_id>`.

The runner plays orchestrator and supervisor: it owns the implement flow and
the sandbox lifecycle, holds RUNNER_BEARER_TOKEN, and is the only component
that talks to both the gateway admin surface and the Docker daemon.
"""

import asyncio
import sys
from pathlib import Path

import typer

from runner.clients import AgentClient, GatewayClient
from runner.config import RunnerSettings
from runner.flow import Manifest, execute_run, read_manifest
from sandbox.backends.docker import DockerBackend

app = typer.Typer(no_args_is_help=True, add_completion=False)


async def _run(settings: RunnerSettings, task_text: str) -> Manifest:
    backend = DockerBackend(
        image=settings.sandbox_image, network=settings.sandbox_network
    )
    async with (
        GatewayClient(settings.mcp_gateway_url, settings.runner_bearer_token) as gateway,
        AgentClient(settings.agent_url) as agent,
    ):
        return await execute_run(settings, task_text, backend, gateway, agent)


@app.command()
def run(task_file: Path) -> None:
    """Execute the implement flow for the task described in TASK_FILE."""
    settings = RunnerSettings()
    task_text = task_file.read_text()
    manifest = asyncio.run(_run(settings, task_text))
    typer.echo(manifest.model_dump_json(indent=2))
    if manifest.status != "done":
        sys.exit(1)


@app.command()
def status(run_id: str) -> None:
    """Print the manifest of a past or running run."""
    settings = RunnerSettings()
    try:
        manifest = read_manifest(settings, run_id)
    except FileNotFoundError:
        typer.echo(f"no manifest for {run_id} under {settings.runs_dir}", err=True)
        sys.exit(1)
    typer.echo(manifest.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
