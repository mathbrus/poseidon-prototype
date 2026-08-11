"""`poseidon blueprint compile|preview|apply`.

The compiler is pure, so the CLI is where the impure parts live: reading the
YAML, reading the registry snapshot, writing the emitted bundle to disk,
submitting the CRs. Apply is exactly compile + submit — reconciling the
cluster stays the operator's alone, and keeping cluster mutation in exactly
one place is what makes the declarative loop trustworthy.
"""

import json
from pathlib import Path

import typer

from compiler import (
    EMPTY,
    InstanceSnapshot,
    RegistrySnapshot,
    compile_blueprint,
    diff_plan,
)
from compiler.errors import CompileError
from schema import load_blueprint
from schema.validate import ValidationError

app = typer.Typer(no_args_is_help=True, help="Compile and preview company blueprints.")


def _load(path: Path) -> dict:
    try:
        return load_blueprint(path)
    except ValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _compile(document: dict, snapshot: RegistrySnapshot):
    try:
        return compile_blueprint(document, snapshot=snapshot)
    except CompileError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _snapshot(live: bool) -> RegistrySnapshot:
    """The registry's view, when asked for. Without `--live` the plan is
    compiled against nothing, which is what you want in CI."""
    if not live:
        return EMPTY
    from registry import open_registry

    try:
        rows = open_registry(migrate=False).query(
            "SELECT agent_id, company, hire, role_template, model_tier, status"
            " FROM registry.agent_instances"
        )
    except Exception as exc:
        # Say so rather than printing a confident "everything is new": the
        # CRs still apply correctly (agent ids are derived, and instances
        # absent from the plan are deleted by name), but the diff is blind.
        typer.echo(
            f"warning: registry unreachable ({exc}); diffing against nothing",
            err=True,
        )
        return EMPTY
    return RegistrySnapshot(instances=tuple(InstanceSnapshot(**row) for row in rows))


@app.command()
def compile(
    blueprint: Path,
    out: Path = typer.Option(
        None, help="Write the emitted Rego bundle here (one file per rule set)"
    ),
    live: bool = typer.Option(False, help="Diff against the live registry"),
) -> None:
    """Compile BLUEPRINT and print the deployment plan as JSON."""
    plan = _compile(_load(blueprint), _snapshot(live))
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        # Clear stale rule files first (like compiler.regolden does): after a
        # firing diff, a leftover role_*.rego would silently widen the bundle.
        for stale in out.glob("*.rego"):
            stale.unlink()
        files = {**plan.bundle.files, **plan.platform_bundle.files}
        for name, body in files.items():
            (out / name).write_text(body)
        typer.echo(f"wrote {len(files)} bundle files to {out}", err=True)
    typer.echo(json.dumps(plan.as_dict(), indent=2, sort_keys=True))


@app.command()
def preview(
    blueprint: Path,
    live: bool = typer.Option(True, help="Diff against the live registry"),
) -> None:
    """Show what applying BLUEPRINT would change, and what it would cost."""
    snapshot = _snapshot(live)
    plan = _compile(_load(blueprint), snapshot)
    typer.echo(diff_plan(plan, snapshot).as_text())


@app.command()
def apply(
    blueprint: Path,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the diff confirmation"),
) -> None:
    """Compile BLUEPRINT and submit the CRs. The operator converges them.

    Apply is exactly compile + submit: the compiler stays pure and the
    operator remains the only thing that mutates a cluster, so "the
    blueprint is the source of truth" is structural rather than a promise.
    """
    snapshot = _snapshot(live=True)
    plan = _compile(_load(blueprint), snapshot)
    diff = diff_plan(plan, snapshot)
    typer.echo(diff.as_text(), err=True)
    if not yes and not diff.empty:
        typer.confirm("apply?", abort=True)

    from orchestrator.cli.crs import submit

    created, updated = submit(plan)
    typer.echo(f"applied {plan.company.name}: {created} created, {updated} updated")
