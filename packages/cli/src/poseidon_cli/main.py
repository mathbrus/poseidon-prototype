"""`poseidon` — the console's command line.

Every command but `login` is one console call with the human's token:
the CLI holds no cluster credentials, reaches no workflow engine and no
database, and performs no act of its own. What it prints is what the
console answered. Two commands touch no console: `login` mints the token,
once, with the kubeconfig the caller already holds — printed, or saved
where every later command finds it when the environment carries none —
and `blueprint new` writes a document on the caller's disk by asking
questions. Neither is an act on a company.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import typer

from common.topology import CONSOLE_TOKEN_AUDIENCE, human_service_account
from poseidon_cli.client import (
    TOKEN_VAR,
    URL_VAR,
    ConsoleClient,
    ConsoleError,
    token_file,
)
from poseidon_cli.follow import follow as follow_events

app = typer.Typer(no_args_is_help=True, add_completion=False)
blueprint_app = typer.Typer(
    no_args_is_help=True, help="Write, preview and apply company blueprints."
)
heartbeat_app = typer.Typer(no_args_is_help=True, help="The company's standing loop.")
company_app = typer.Typer(no_args_is_help=True, help="The company this cluster hosts.")
app.add_typer(blueprint_app, name="blueprint")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(company_app, name="company")

DEFAULT_PLATFORM_NAMESPACE = "poseidon-platform"


def _client() -> ConsoleClient:
    client = ConsoleClient.from_env()
    if not client.token:
        typer.echo(
            f"no token: `poseidon login <name> --save` keeps one at {token_file()},"
            f" or set {TOKEN_VAR} (`export {TOKEN_VAR}=$(poseidon login <name>)`);"
            f" set {URL_VAR} if the console is not at {client.base_url}",
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
        if exc.status == 401 and getattr(fn, "__self__", None) is not None:
            client = fn.__self__
            if getattr(client, "token_source", "") == "file":
                typer.echo(
                    f"the saved token at {token_file()} was refused — expired or"
                    " revoked; `poseidon login <name> --save` writes a fresh one",
                    err=True,
                )
        raise typer.Exit(1) from exc


@app.command()
def login(
    name: str,
    namespace: str = typer.Option(
        DEFAULT_PLATFORM_NAMESPACE, help="The platform namespace the chart installed into"
    ),
    duration: int = typer.Option(3600, help="Token lifetime, seconds"),
    save: bool = typer.Option(
        False,
        "--save",
        help="Keep the token in your config directory instead of printing it;"
        " every later command reads it there when POSEIDON_TOKEN is unset",
    ),
) -> None:
    """Mint a token for a human the chart declared, with your own cluster
    credentials, and print it (`export POSEIDON_TOKEN=$(poseidon login owner)`)
    or save it (`poseidon login owner --save`). The token is for the
    console's audience only — worthless anywhere else."""
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
    if not save:
        typer.echo(token.status.token)
        return
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(token.status.token + "\n")
    expires = token.status.expiration_timestamp
    typer.echo(f"saved a token for {name} at {path}, valid until {expires}", err=True)


@app.command()
def whoami() -> None:
    """Who the console takes you for, and which company it hosts."""
    _show(_call(_client().whoami))


@blueprint_app.command("new")
def blueprint_new(
    path: Path = typer.Argument(
        None, help="Where to write; default <company-name>.yaml in the current directory"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Write a blueprint by answering questions, section by section, with
    the shipped example's answers as defaults. What you decide — the
    name, the vision, who may act, the repository, the gate, the money —
    is asked; the shape every company repeats is written; the result is
    compiled as the console would before a byte lands on disk."""
    from poseidon_cli import new

    try:
        answers = new.ask(new.QuestionaryPrompter())
    except KeyboardInterrupt:
        typer.echo("nothing written", err=True)
        raise typer.Exit(130) from None
    document = new.build(answers)
    problems = new.problems(document)
    if problems:
        typer.echo(
            "the answers do not compile:\n  - " + "\n  - ".join(problems), err=True
        )
        raise typer.Exit(1)
    target = path or Path(f"{answers.name}.yaml")
    if target.exists() and not force:
        typer.echo(f"{target} exists; --force overwrites it", err=True)
        raise typer.Exit(1)
    target.write_text(new.render(document))
    typer.echo(f"wrote {target}")
    typer.echo(
        f"next: `poseidon blueprint apply {target}` on a cluster that hosts {answers.name},"
        f" or `BLUEPRINT={target} deploy/kind/kind-up.sh` for a fresh kind cluster"
    )


@blueprint_app.command()
def preview(blueprint: Path) -> None:
    """Show what applying BLUEPRINT would change, and what it would cost."""
    result = _call(_client().preview, blueprint.read_text())
    typer.echo(result["diff"])


CONVERGE_TIMEOUT_S = 600


@blueprint_app.command()
def apply(
    blueprint: Path,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the diff confirmation"),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait until the company is converged — OPA serves its policy — before returning",
    ),
    wait_timeout: int = typer.Option(
        CONVERGE_TIMEOUT_S, help="Seconds to wait for `converged` before giving up"
    ),
) -> None:
    """Apply BLUEPRINT: the console compiles it against the live registry
    and submits the CRs; the operator converges them, and this waits until
    it has — the company is live only once OPA serves its policy."""
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
    if wait:
        _await_converged(client, int(result.get("generation", 0)), wait_timeout)


def _await_converged(client: ConsoleClient, generation: int, timeout_s: int) -> None:
    """Poll the company's phase until it reads `converged` for at least the
    generation this apply made; say so, with the time it took, or say
    where to look when it does not come."""
    started = time.monotonic()
    said_activating = False
    while True:
        state = _call(client.company)
        if state["phase"] == "converged" and state["observedGeneration"] >= generation:
            typer.echo(f"converged ({int(time.monotonic() - started)}s)")
            return
        if not said_activating:
            typer.echo("activating …", err=True)
            said_activating = True
        if time.monotonic() - started >= timeout_s:
            typer.echo(
                f"still {state['phase']} after {timeout_s}s"
                + (f": {state['message']}" if state.get("message") else "")
                + " — `poseidon company show`, or `kubectl get company`",
                err=True,
            )
            raise typer.Exit(1)
        time.sleep(2)


@company_app.command("show")
def company_show() -> None:
    """The applied company as its CR tells it: phase, generation, bundle."""
    _show(_call(_client().company))


@company_app.command("delete")
def company_delete(
    reason: str = typer.Option("", help="Why, recorded with the act"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
) -> None:
    """Stop the company — the one stop there is. The console decides the
    act under your name, cancels every running run, deletes the Company
    and waits for the teardown. The repository and the ledgers stay:
    applying the same blueprint again resumes the company."""
    client = _client()
    company = _call(client.whoami).get("company")
    if not company:
        typer.echo("no company is applied", err=True)
        raise typer.Exit(1)
    running = [r["run_id"] for r in _call(client.runs) if r.get("status") == "running"]
    typer.echo(
        f"{company}: {len(running)} running run(s)"
        + (f" ({', '.join(running)})" if running else "")
        + " — they will be cancelled",
        err=True,
    )
    if not yes:
        typer.confirm(f"delete {company}?", abort=True)
    _show(_call(client.delete_company, reason))


@app.command()
def run(
    issue_number: int,
    detach: bool = typer.Option(False, help="Start the run and exit"),
) -> None:
    """Start one implement run by hand on a ticket the company filed — a
    person reproducing a run. The heartbeat starts the company's runs on
    its own."""
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
    """Start one review run by hand on a delivered pull request — a person
    reproducing a run, like `run`."""
    _start("review", pr_number, detach)


# How long a just-started run may take to have a row and a running
# workflow before waiting on it is given up.
START_GRACE_S = 120.0


def _start(workflow: str, number: int, detach: bool) -> None:
    client = _client()
    started = _call(client.start_run, workflow, number)
    typer.echo(f"started {started['workflow_id']} (run_id {started['run_id']})", err=True)
    if detach:
        return
    _show(_await_run(client, started["run_id"]))


def _await_run(client: ConsoleClient, run_id: str) -> dict[str, Any]:
    """Follow the run to its end, printing its rows as they land. The
    worker writes the run's row a moment after the start is accepted, so
    "no such run" is "not yet" within the grace, and a refusal after it."""
    born = time.monotonic()
    record: dict[str, Any] = {}

    def ended() -> bool:
        nonlocal record
        try:
            record = client.run(run_id)
        except ConsoleError as exc:
            if exc.status == 422 and time.monotonic() - born < START_GRACE_S:
                return False
            raise
        execution = record["execution"]["status"]
        if record["status"] != "running" or execution not in ("RUNNING", "NOT_FOUND"):
            return True
        if execution == "NOT_FOUND" and time.monotonic() - born >= START_GRACE_S:
            typer.echo(
                f"run {run_id} has no workflow after {START_GRACE_S:.0f}s", err=True
            )
            raise typer.Exit(1)
        return False

    follow_events(
        client, lambda line: typer.echo(line, err=True), run_id=run_id, stop=ended
    )
    if record.get("status") != "done":
        _show(record)
        sys.exit(1)
    return record


@app.command()
def signal(
    run_id: str,
    gate: str,
    approved: bool = typer.Option(True, help="The verdict: --approved or --no-approved"),
    payload: str = typer.Option(None, help="A note recorded with the verdict"),
) -> None:
    """Answer a gate a run parked on — `merge` or `budget`. The console
    records the verdict under your name, then delivers it. Approving a
    budget pause lets the run continue under its cap as it stands: raise
    the cap in the blueprint first if it needs more."""
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
def follow(
    run: str = typer.Option(None, help="Only this run's rows"),
    after: int = typer.Option(0, help="Resume from this cursor (the last id printed)"),
    json_lines: bool = typer.Option(
        False, "--json", help="The rows as stored, one JSON object per line"
    ),
) -> None:
    """Watch the company: every tick, run, dispatch, tool call, decision,
    gate and verdict as it lands, one line each, and the spend whenever
    it moves — until you interrupt. Reads only."""
    client = _client()
    _call(
        lambda: follow_events(client, typer.echo, after=after, run_id=run, raw=json_lines)
    )


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
