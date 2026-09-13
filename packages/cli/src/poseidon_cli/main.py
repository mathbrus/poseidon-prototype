"""`poseidon` — the console's command line.

Every command but `login` is one console call with the human's token:
the CLI holds no cluster credentials, reaches no workflow engine and no
database, and performs no act of its own. What it prints is what the
console answered. Two commands touch no console: `login` mints the token,
once, with the kubeconfig the caller already holds — printed, or saved
where every later command finds it when the environment carries none —
and `blueprint new` writes a document on the caller's disk by asking
questions. Neither is an act on a company.

The tree is the glossary's nouns: the blueprint, the company, its
heartbeat, its runs; one verb per meaning (`show` to read, `start` to
begin a run by hand, `verdict` to answer a gate); `--reason` on every act
that commits the company to something; `--no-wait` wherever a command
would otherwise wait for the cluster.
"""

import json
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

import typer

from common.topology import (
    CONSOLE_TOKEN_AUDIENCE,
    PLATFORM_NAMESPACE,
    human_service_account,
)
from common.workflows import GATES
from poseidon_cli.client import (
    TOKEN_VAR,
    URL_VAR,
    ConsoleClient,
    ConsoleError,
    token_file,
)
from poseidon_cli.follow import follow as follow_events

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Act on the company this cluster hosts, and read it, through the console.",
)
blueprint_app = typer.Typer(
    no_args_is_help=True, help="Write, preview and apply a company blueprint."
)
company_app = typer.Typer(
    no_args_is_help=True, help="The company this cluster hosts: show it, or delete it."
)
heartbeat_app = typer.Typer(
    no_args_is_help=True, help="The company's standing loop: show it, or wake it."
)
run_app = typer.Typer(
    no_args_is_help=True, help="One run: show it, read its story, cancel it."
)
start_app = typer.Typer(
    no_args_is_help=True,
    help="Start a run by hand, on a ticket or a pull request. An operator's tool:"
    " the heartbeat starts the company's runs on its own.",
)
app.add_typer(blueprint_app, name="blueprint")
app.add_typer(company_app, name="company")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(run_app, name="run")
app.add_typer(start_app, name="start")


class Verdict(str, Enum):
    approve = "approve"
    reject = "reject"


# The gates a run can park on, as an argument's closed choice list.
Gate = Enum("Gate", {gate: gate for gate in GATES}, type=str)

REASON_OPTION = typer.Option(
    ..., "--reason", help="Why. Recorded with the act, under your name."
)
NO_WAIT_OPTION = typer.Option(
    False, "--no-wait", help="Return as soon as the console has accepted the request."
)


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


# -- you -----------------------------------------------------------------------


@app.command()
def login(
    name: str = typer.Argument(
        ..., help="A human the chart declares (on kind: owner, auditor)."
    ),
    namespace: str = typer.Option(
        PLATFORM_NAMESPACE, help="The namespace the platform is installed into."
    ),
    duration: int = typer.Option(3600, help="How long the token lives, in seconds."),
    save: bool = typer.Option(
        False,
        "--save",
        help="Save the token in your config directory instead of printing it;"
        " every later command reads it there when POSEIDON_TOKEN is unset.",
    ),
) -> None:
    """Mint a console token for a human the chart declares, with the
    kubeconfig you already hold (a kind cluster; elsewhere your identity
    provider signs you in). Prints the token, or saves it with --save.
    """
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


# -- the blueprint -------------------------------------------------------------


@blueprint_app.command("new")
def blueprint_new(
    path: Path = typer.Argument(
        None, help="Where to write; default <company-name>.yaml in the current directory."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
) -> None:
    """Write a blueprint by answering questions, section by section.
    Compiles the answers before writing; prints the file written and the
    command that applies it. Nothing reaches the cluster."""
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
    typer.echo(f"next: `poseidon blueprint apply {target}`")


@blueprint_app.command()
def preview(blueprint: Path = typer.Argument(..., help="The blueprint file.")) -> None:
    """Show what applying BLUEPRINT would change — the diff against what
    the cluster hosts, and the budget. Changes nothing."""
    result = _call(_client().preview, blueprint.read_text())
    typer.echo(result["diff"])


CONVERGE_TIMEOUT_S = 600


@blueprint_app.command()
def apply(
    blueprint: Path = typer.Argument(..., help="The blueprint file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
    no_wait: bool = NO_WAIT_OPTION,
    wait_timeout: int = typer.Option(
        CONVERGE_TIMEOUT_S, help="Seconds to wait for the company to converge."
    ),
) -> None:
    """Apply BLUEPRINT: shows the diff, asks, applies through the console,
    then waits until the company is converged — live, its policy served —
    and says how long that took. Prints what was created and updated."""
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
    if not no_wait:
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


# -- the company ---------------------------------------------------------------


@company_app.command("show")
def company_show() -> None:
    """The applied company: its name, phase (`activating` until its policy
    is served, `converged` after), generation and bundle revision."""
    _show(_call(_client().company))


@company_app.command("delete")
def company_delete(
    reason: str = REASON_OPTION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
) -> None:
    """Delete the company: cancels its running runs, removes it and waits
    for the teardown. Asks first, naming the runs it will cancel. The one
    way to stop a company; applying its blueprint again resumes it."""
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


# -- the heartbeat -------------------------------------------------------------


@heartbeat_app.command("show")
def heartbeat_show(
    since: str = typer.Option(None, help="Only ticks at or after this timestamp."),
) -> None:
    """The standing loop: its execution, and every tick — what it saw and
    what it started, or why it did nothing."""
    _show(_call(_client().heartbeat, since))


@heartbeat_app.command()
def wake() -> None:
    """Ask the loop to look now instead of at its next check-in. It
    decides for itself what, if anything, to start."""
    _show(_call(_client().wake))


# -- runs ------------------------------------------------------------------------


@app.command()
def runs(
    since: str = typer.Option(None, help="Only runs started at or after this timestamp."),
) -> None:
    """The company's runs: id, workflow, task, status, when."""
    _show(_call(_client().runs, since))


@run_app.command("show")
def run_show(run_id: str = typer.Argument(..., help="The run's id.")) -> None:
    """One run: its record — task, status, attempts, what it delivered —
    and its workflow's execution."""
    _show(_call(_client().run, run_id))


@run_app.command("story")
def run_story(
    run_id: str = typer.Argument(..., help="The run's id."),
    decisions: bool = typer.Option(
        False,
        "--decisions",
        help="Only the rows that carry a rationale: what was decided, and why.",
    ),
) -> None:
    """The run's story: every audit row, from every source, in order —
    each tool call, decision, gate and verdict, with the actor's rationale
    where one was stated."""
    _show(_call(_client().story, run_id, decisions))


@run_app.command("cancel")
def run_cancel(
    run_id: str = typer.Argument(..., help="The run's id."),
    reason: str = REASON_OPTION,
) -> None:
    """Cancel a run. The console records the act and your reason under
    your name, then stops the workflow; the run's sandbox and agents are
    torn down."""
    _show(_call(_client().cancel, run_id, reason))


@start_app.command("implement")
def start_implement(
    ticket: int = typer.Argument(..., help="The ticket's number on the repository."),
    no_wait: bool = NO_WAIT_OPTION,
) -> None:
    """Start an implement run on a ticket and follow it to its end,
    printing its rows as they land; exits non-zero if the run fails."""
    _start("implement", ticket, no_wait)


@start_app.command("review")
def start_review(
    pr: int = typer.Argument(..., help="The pull request's number on the repository."),
    no_wait: bool = NO_WAIT_OPTION,
) -> None:
    """Start a review run on a pull request and follow it to its end. The
    run parks on its merge gate until `poseidon verdict <run_id> merge
    approve|reject` answers, or policy merges on its own."""
    _start("review", pr, no_wait)


# How long a just-started run may take to have a row and a running
# workflow before waiting on it is given up.
START_GRACE_S = 120.0


def _start(workflow: str, number: int, no_wait: bool) -> None:
    client = _client()
    started = _call(client.start_run, workflow, number)
    typer.echo(f"started {started['workflow_id']} (run_id {started['run_id']})", err=True)
    if no_wait:
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


# -- the human's answer --------------------------------------------------------


@app.command()
def verdict(
    run_id: str = typer.Argument(..., help="The run parked on the gate."),
    gate: Gate = typer.Argument(..., help="The gate: merge, or budget."),
    verdict: Verdict = typer.Argument(..., help="approve or reject."),
    reason: str = REASON_OPTION,
) -> None:
    """Answer a gate a run parked on. The console records the verdict and
    your reason under your name, then delivers it. Approving a budget pause
    lets the run continue under its cap as it stands."""
    _show(
        _call(_client().resolve, run_id, gate.value, verdict is Verdict.approve, reason)
    )


# -- the window ------------------------------------------------------------------


@app.command()
def follow(
    run: str = typer.Option(None, help="Only this run's rows."),
    after: int = typer.Option(0, help="Resume from this cursor (the last id printed)."),
    json_lines: bool = typer.Option(
        False, "--json", help="The rows as stored, one JSON object per line."
    ),
) -> None:
    """Watch the company: every audit row as it lands — ticks, runs, tool
    calls, decisions, gates, verdicts — one line each, the spend whenever
    it moves, until you interrupt."""
    client = _client()
    _call(
        lambda: follow_events(client, typer.echo, after=after, run_id=run, raw=json_lines)
    )


@app.command()
def spend(
    run_id: str = typer.Argument(None, help="A run's id; omit for the company's."),
    since: str = typer.Option(
        None, help="Company-wide: only spend at or after this timestamp."
    ),
) -> None:
    """Spend to date: the company's, or one run's with RUN_ID — the
    total, and every model call behind it."""
    client = _client()
    if run_id:
        _show(_call(client.spend, run_id))
    else:
        _show(_call(client.spend_since, since))


if __name__ == "__main__":
    app()
