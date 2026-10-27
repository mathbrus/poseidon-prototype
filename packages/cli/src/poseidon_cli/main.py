"""`poseidon` — the console's command line.

Every command but `login` is one console call with the human's token:
the CLI holds no cluster credentials, reaches no workflow engine and no
database, and performs no act of its own. What it prints is what the
console answered — rendered for a person by default, verbatim with
`--json` on every read. Two commands touch no console: `login` mints the
token, once, with the kubeconfig the caller already holds — printed, or
saved where every later command finds it when the environment carries
none — and `blueprint new` writes a document on the caller's disk by
asking questions. Neither is an act on a company.

The tree is the glossary's nouns: the blueprint, the company, its
heartbeat, its runs; one verb per meaning (`show` to read, `start` to
begin a run by hand, `verdict` to answer a gate, `pause` and `resume` to
hold the company and let it go); `--reason` on every act that owes a
rationale (the console's acts table says which); `--no-wait` wherever a
command would otherwise wait for the cluster.
"""

import importlib.metadata
import json
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich.text import Text

from common.console import (
    EXECUTION_NOT_FOUND,
    EXECUTION_RUNNING,
    CompanyPhase,
    Gate,
    RunDetail,
    RunStatus,
    RunView,
    TickView,
)
from common.repos import repo_slug
from common.topology import (
    CONSOLE_TOKEN_AUDIENCE,
    PLATFORM_NAMESPACE,
    human_service_account,
)
from common.workflows import GATE_APPROVED, GATE_REJECTED, HEARTBEAT_PAUSED
from poseidon_cli import render
from poseidon_cli.client import (
    TOKEN_VAR,
    URL_VAR,
    ConsoleClient,
    ConsoleError,
    token_file,
)
from poseidon_cli.follow import follow as follow_events


def examples(*lines: str) -> str:
    """A command's examples, as the epilog its `--help` ends with. The
    docs site renders the CLI reference's examples from the same text."""
    return "Examples:\n\n" + "\n\n".join(f"  {line}" for line in lines)


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
    no_args_is_help=True,
    help="The company's standing loop: show it, or wake it. `poseidon pause` holds it.",
)
run_app = typer.Typer(
    no_args_is_help=True, help="One run: show it, read its story, cancel it."
)
start_app = typer.Typer(
    no_args_is_help=True,
    help="Start a run by hand, on a ticket or a pull request. A person's tool:"
    " the heartbeat starts the company's runs on its own.",
)
app.add_typer(blueprint_app, name="blueprint")
app.add_typer(company_app, name="company")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(run_app, name="run")
app.add_typer(start_app, name="start")


class Verdict(StrEnum):
    approve = "approve"
    reject = "reject"


REASON_OPTION = typer.Option(
    ..., "--reason", help="Why. Recorded with the act, under your name."
)
NO_WAIT_OPTION = typer.Option(
    False, "--no-wait", help="Return as soon as the console has accepted the request."
)
JSON_OPTION = typer.Option(
    False, "--json", help="Print exactly what the console answered, as JSON."
)


def _client() -> ConsoleClient:
    client = ConsoleClient.from_env()
    if not client.token:
        render.error(
            f"no token: `poseidon login <name> --save` keeps one at {token_file()},"
            f" or set {TOKEN_VAR} (`export {TOKEN_VAR}=$(poseidon login <name>)`);"
            f" set {URL_VAR} if the console is not at {client.base_url}"
        )
        raise typer.Exit(2)
    return client


def _json(payload: BaseModel | dict[str, Any]) -> None:
    """The console's answer, as it answered it."""
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    typer.echo(json.dumps(payload, indent=2))


def _call(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except ConsoleError as exc:
        render.error(exc.detail, getattr(exc, "rule", None))
        if exc.status == 401 and getattr(fn, "__self__", None) is not None:
            client = fn.__self__
            if getattr(client, "token_source", "") == "file":
                render.error(
                    f"the saved token at {token_file()} was refused — expired or"
                    " revoked; `poseidon login <name> --save` writes a fresh one"
                )
        raise typer.Exit(1) from exc


# -- you -----------------------------------------------------------------------


@app.command(
    epilog=examples(
        "poseidon login owner --save", "export POSEIDON_TOKEN=$(poseidon login auditor)"
    )
)
def login(
    name: str = typer.Argument(
        ..., help="A human the chart declares (on kind: owner, auditor, eval)."
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
        render.error(
            f"could not mint a token for {human_service_account(name)!r} in"
            f" {namespace!r}: {exc.reason}"
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
    render.act(f"saved a token for {name} at {path}, valid until {expires}", render.ERR)


@app.command(epilog=examples("poseidon version"))
def version(as_json: bool = JSON_OPTION) -> None:
    """This CLI's version, and the version the console runs — the release
    the cluster is on. Needs no token: the health route is open."""
    cli_version = importlib.metadata.version("poseidon-cli")
    client = ConsoleClient.from_env()
    try:
        console_version = client.healthz().version
    except (ConsoleError, httpx.HTTPError):
        console_version = None
    if as_json:
        _json({"cli": cli_version, "console": console_version})
        return
    render.act(f"cli {cli_version}")
    if console_version:
        render.act(f"console {console_version}")
    else:
        render.act(Text(f"console unreachable at {client.base_url}", style=render.DIM))


@app.command(epilog=examples("poseidon whoami"))
def whoami(as_json: bool = JSON_OPTION) -> None:
    """Who the console takes you for, and which company it hosts."""
    client = _client()
    who = _call(client.whoami)
    if as_json:
        _json(who)
        return
    render.act(f"you are {who.human} ({who.lifecycle})")
    company = who.company
    if not company:
        render.act(Text("no company applied", style=render.DIM))
        return
    try:
        state = client.company()
    except ConsoleError:
        render.act(f"company: {company}")
        return
    render.act(Text.assemble(f"company: {company} · ", render.status(state.phase)))


# -- the blueprint -------------------------------------------------------------


@blueprint_app.command(
    "new",
    epilog=examples(
        "poseidon blueprint new", "poseidon blueprint new company.yaml --force"
    ),
)
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
        render.error("nothing written")
        raise typer.Exit(130) from None
    document = new.build(answers)
    problems = new.problems(document)
    if problems:
        render.error("the answers do not compile:\n  - " + "\n  - ".join(problems))
        raise typer.Exit(1)
    target = path or Path(f"{answers.name}.yaml")
    if target.exists() and not force:
        render.error(f"{target} exists; --force overwrites it")
        raise typer.Exit(1)
    target.write_text(new.render(document))
    render.act(f"wrote {target}")
    render.act(Text(f"next: poseidon blueprint apply {target}", style=render.DIM))


@blueprint_app.command(
    "show",
    epilog=examples(
        "poseidon blueprint show > company.yaml",
        "poseidon blueprint show --json",
    ),
)
def blueprint_show(as_json: bool = JSON_OPTION) -> None:
    """The blueprint the company was applied from, exactly as it was
    written: applying the output again changes nothing. --json adds who
    applied it, when, and whether policy enforces it yet."""
    view = _call(_client().blueprint)
    if as_json:
        _json(view)
        return
    typer.echo(view.text, nl=False)


@blueprint_app.command(epilog=examples("poseidon blueprint preview company.yaml"))
def preview(blueprint: Path = typer.Argument(..., help="The blueprint file.")) -> None:
    """Show what applying BLUEPRINT would change — the diff against what
    the cluster hosts, and the budget. Changes nothing."""
    result = _call(_client().preview, blueprint.read_text())
    render.diff(result.diff)


CONVERGE_TIMEOUT_S = 600


@blueprint_app.command(
    epilog=examples(
        "poseidon blueprint apply company.yaml",
        "poseidon blueprint apply company.yaml --yes --no-wait",
    )
)
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
    diff = _call(client.preview, text).diff
    render.diff(diff, render.ERR)
    if not yes and "no changes" not in diff:
        typer.confirm("apply?", abort=True)
    result = _call(client.apply, text)
    render.act(
        Text.assemble(
            f"applied {result.company}: {result.created} created,"
            f" {result.updated} updated ",
            render.dim(f"({result.rule})"),
        )
    )
    if not no_wait:
        _await_converged(client, result.generation, wait_timeout)


def _await_converged(client: ConsoleClient, generation: int, timeout_s: int) -> None:
    """Poll the company's phase until it reads `converged` for at least the
    generation this apply made; say so, with the time it took, or say
    where to look when it does not come."""
    started = time.monotonic()
    said_activating = False
    while True:
        state = _call(client.company)
        if (
            state.phase == CompanyPhase.converged
            and state.observed_generation >= generation
        ):
            render.act(
                Text.assemble(
                    render.status(state.phase),
                    render.dim(f" ({render.duration(time.monotonic() - started)})"),
                )
            )
            return
        if not said_activating:
            render.act(Text.assemble(render.status(state.phase), " …"), render.ERR)
            said_activating = True
        if time.monotonic() - started >= timeout_s:
            render.error(
                f"still {state.phase} after {timeout_s}s"
                + (f": {state.message}" if state.message else "")
                + " — `poseidon company show`, or `kubectl get company`"
            )
            raise typer.Exit(1)
        time.sleep(2)


# -- the company ---------------------------------------------------------------


@company_app.command("show", epilog=examples("poseidon company show"))
def company_show(as_json: bool = JSON_OPTION) -> None:
    """The applied company: its name, phase (`activating` until its policy
    is served, `converged` after), generation and bundle revision, the
    repository it works on, and who holds its heartbeat paused, if
    anyone."""
    state = _call(_client().company)
    if as_json:
        _json(state)
        return
    paused = state.paused
    render.record(
        [
            ("company", state.company),
            ("phase", render.status(state.phase)),
            (
                "generation",
                f"{state.generation} (observed {state.observed_generation})",
            ),
            ("bundle", render.dim(state.bundle_revision)),
            ("repository", state.repo or ""),
            (
                "heartbeat",
                Text.assemble(
                    render.status(HEARTBEAT_PAUSED),
                    f" by {paused.by}, {render.ago(paused.at)}",
                )
                if paused
                else render.status(RunStatus.running),
            ),
            ("message", state.message),
        ]
    )


# What `--purge-remote` does to the repository, as the help and the
# warning before the question both say it.
PURGE_EFFECT = (
    "every open pull request and issue closed, every branch but the base and"
    " default ones deleted, and those forced to one empty root commit"
)


@company_app.command(
    "delete",
    epilog=examples(
        'poseidon company delete --reason "wrong vision"',
        'poseidon company delete --reason "done" --yes',
        'poseidon company delete --reason "start over" --purge-remote',
    ),
)
def company_delete(
    reason: str = REASON_OPTION,
    purge_remote: bool = typer.Option(
        False,
        "--purge-remote",
        help=f"Also reset the company's repository in place: {PURGE_EFFECT}.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
) -> None:
    """Delete the company: holds its heartbeat, cancels its running runs,
    removes it and erases every record the platform kept of it — runs,
    tickets, spend, audit rows, workflow histories. Asks first, naming the
    runs it will cancel; with `--purge-remote`, names the repository and
    asks you to type its `owner/name`. The one way to stop a company
    (`pause` holds it instead); applying its blueprint again starts a new
    company, with no history."""
    client = _client()
    company = _call(client.whoami).company
    if not company:
        render.error("no company is applied")
        raise typer.Exit(1)
    running = [r for r in _call(client.runs).runs if r.status == RunStatus.running]
    render.act(
        f"{company}: {len(running)} running run(s)"
        + (" — they will be cancelled:" if running else ""),
        render.ERR,
    )
    if running:
        render.table(
            ("run", "workflow", "task"),
            [(r.run_id, r.workflow, r.task) for r in running],
            render.ERR,
        )
    if purge_remote:
        repo = _call(client.company).repo or ""
        render.act(
            f"--purge-remote resets {repo} in place: {PURGE_EFFECT}. Closed"
            " issues and pull requests stay visible on the git host.",
            render.ERR,
        )
        if not yes and typer.prompt(f"type {repo_slug(repo)} to purge it") != repo_slug(
            repo
        ):
            render.error("that is not the repository's name; nothing was deleted")
            raise typer.Exit(1)
    elif not yes:
        typer.confirm(
            f"delete {company} and erase everything recorded about it?", abort=True
        )
    result = _call(client.delete_company, reason, purge_remote)
    cancelled = result.runs_cancelled
    render.act(
        f"{result.company} deleted in {render.duration(result.elapsed_s)}"
        + ("; its repository purged" if result.purged_remote else "")
        + (f"; cancelled {', '.join(cancelled)}" if cancelled else "")
    )


# -- the heartbeat -------------------------------------------------------------


@heartbeat_app.command("show", epilog=examples("poseidon heartbeat show"))
def heartbeat_show(
    since: str = typer.Option(None, help="Only ticks at or after this timestamp."),
    as_json: bool = JSON_OPTION,
) -> None:
    """The standing loop: its execution, and every tick — what it saw and
    what it started, or why it did nothing."""
    state = _call(_client().heartbeat, since)
    if as_json:
        _json(state)
        return
    execution = state.execution
    ticks = state.ticks
    line = Text.assemble("heartbeat ", render.status(execution.status))
    if execution.state:
        line.append(f" ({execution.state})", style=render.DIM)
    if ticks:
        line.append(f", last tick {render.ago(ticks[-1].ts)}")
    render.act(line)
    render.table(
        ("when", "outcome", "detail"),
        [_tick_row(tick) for tick in reversed(ticks)],
        empty="no ticks yet",
    )


def _tick_row(tick: TickView) -> tuple[Any, ...]:
    """The outcome is the coloured word, the detail says what (or why not)."""
    return (render.dim(render.ago(tick.ts)), render.status(tick.outcome), tick.detail)


@heartbeat_app.command(epilog=examples("poseidon heartbeat wake"))
def wake() -> None:
    """Ask the loop to look now instead of at its next check-in. It
    decides for itself what, if anything, to start."""
    _call(_client().wake)
    render.act("heartbeat woken")


@app.command(
    epilog=examples('poseidon pause --reason "reading the ledger with nothing moving"')
)
def pause(reason: str = REASON_OPTION) -> None:
    """Hold the company: its heartbeat starts nothing until `resume`, and
    everything stays where it is to be read. Runs already in flight
    finish; you may still start a run, answer a gate or cancel by hand.
    Recorded under your name with your reason."""
    result = _call(_client().pause, reason)
    render.act(f"heartbeat paused by {result.paused.by if result.paused else ''}")


@app.command(epilog=examples('poseidon resume --reason "read it"'))
def resume(reason: str = REASON_OPTION) -> None:
    """Lift the pause: the heartbeat looks at the world at once and
    decides for itself what to start. Recorded under your name with your
    reason."""
    _call(_client().resume, reason)
    render.act("heartbeat resumed")


# -- runs ------------------------------------------------------------------------


def _elapsed(record: RunView) -> str:
    if record.created_at is None:
        return ""
    ended = record.finished_at or datetime.now(UTC)
    return render.duration((ended - record.created_at).total_seconds())


@app.command(epilog=examples("poseidon runs", "poseidon runs --json"))
def runs(
    since: str = typer.Option(None, help="Only runs started at or after this timestamp."),
    as_json: bool = JSON_OPTION,
) -> None:
    """The company's runs, newest first: id, workflow, task, status, when."""
    listing = _call(_client().runs, since)
    if as_json:
        _json(listing)
        return
    # The runs still going first, then the rest newest first.
    newest_first = sorted(
        listing.runs, key=lambda r: str(r.created_at or ""), reverse=True
    )
    ordered = [r for r in newest_first if r.status == RunStatus.running] + [
        r for r in newest_first if r.status != RunStatus.running
    ]
    render.table(
        ("run", "workflow", "task", "status", "started", "took"),
        [
            (
                r.run_id,
                r.workflow,
                r.task,
                render.status(r.status),
                render.dim(render.ago(r.created_at)),
                render.dim(_elapsed(r)),
            )
            for r in ordered
        ],
        empty="no runs yet",
    )


@run_app.command("show", epilog=examples("poseidon run show auto-3d73e674"))
def run_show(
    run_id: str = typer.Argument(..., help="The run's id."), as_json: bool = JSON_OPTION
) -> None:
    """One run: its record — task, status, iterations, what it delivered —
    and its workflow's execution."""
    record = _call(_client().run, run_id)
    if as_json:
        _json(record)
        return
    _render_run(record)


def _render_run(record: RunDetail) -> None:
    execution = record.execution
    workflow_line = Text.assemble(
        "workflow ",
        render.status(execution.status),
        render.dim(f" {execution.workflow_id}"),
    )
    if execution.state:
        workflow_line.append(f" ({execution.state})", style=render.DIM)
    render.record(
        [
            ("run", record.run_id),
            ("workflow", record.workflow),
            ("task", record.task),
            ("status", render.status(record.status)),
            ("started", render.dim(render.ago(record.created_at))),
            ("took", render.dim(_elapsed(record))),
            ("iterations", record.iterations or None),
            ("pull request", record.pr_url),
            ("settled", _settled(record)),
            ("tickets", ", ".join(record.ticket_urls) or None),
            ("verdict", render.status(record.verdict) if record.verdict else None),
            ("error", Text(record.error, style="red") if record.error else None),
            ("execution", workflow_line),
        ]
    )


def _settled(record: RunDetail) -> Text | None:
    """What became of the run's pull request, and why when it was refused."""
    if record.pr_settled is None:
        return None
    settled = render.status(record.pr_settled)
    if record.pr_settled_reason:
        settled.append(f" ({record.pr_settled_reason})", style=render.DIM)
    return settled


@run_app.command(
    "story",
    epilog=examples(
        "poseidon run story auto-3d73e674",
        "poseidon run story heartbeat-hello-saas --decisions",
    ),
)
def run_story(
    run_id: str = typer.Argument(..., help="The run's id."),
    decisions: bool = typer.Option(
        False,
        "--decisions",
        help="Only the rows that carry a rationale: what was decided, and why.",
    ),
    as_json: bool = JSON_OPTION,
) -> None:
    """The run's story: every audit row, from every source, in order —
    each tool call, decision, gate and verdict, with the actor's rationale
    where one was stated."""
    story = _call(_client().story, run_id, decisions)
    if as_json:
        _json(story)
        return
    if not story.events:
        render.act(Text("no rows yet", style=render.DIM))
    for event in story.events:
        render.OUT.print(render.row(event))


@run_app.command(
    "cancel",
    epilog=examples('poseidon run cancel auto-3d73e674 --reason "superseded by #43"'),
)
def run_cancel(
    run_id: str = typer.Argument(..., help="The run's id."),
    reason: str = REASON_OPTION,
) -> None:
    """Cancel a run. The console records the act and your reason under
    your name, then stops the workflow; the run's sandbox and agents are
    torn down."""
    result = _call(_client().cancel, run_id, reason)
    render.act(f"run {result.run_id} cancelled")


@start_app.command(
    "implement",
    epilog=examples(
        "poseidon start implement 42", "poseidon start implement 42 --no-wait"
    ),
)
def start_implement(
    ticket: int = typer.Argument(..., help="The ticket's number on the repository."),
    no_wait: bool = NO_WAIT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Start an implement run on a ticket and follow it to its end,
    printing its rows as they land and then its record; exits non-zero if
    the run fails."""
    _start("implement", ticket, no_wait, as_json)


@start_app.command("review", epilog=examples("poseidon start review 43 --no-wait"))
def start_review(
    pr: int = typer.Argument(..., help="The pull request's number on the repository."),
    no_wait: bool = NO_WAIT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Start a review run on a pull request and follow it to its end. The
    run parks on its merge gate until `poseidon verdict <run_id> merge
    approve|reject` answers, or automation merges it."""
    _start("review", pr, no_wait, as_json)


# How long a just-started run may take to have a row and a running
# workflow before waiting on it is given up.
START_GRACE_S = 120.0


def _start(workflow: str, number: int, no_wait: bool, as_json: bool) -> None:
    client = _client()
    started = _call(client.start_run, workflow, number)
    render.act(
        Text.assemble(
            f"started {started.workflow_id} ", render.dim(f"(run {started.run_id})")
        ),
        render.ERR,
    )
    if no_wait:
        if as_json:
            _json(started)
        return
    record = _await_run(client, started.run_id)
    if as_json:
        _json(record)
    else:
        _render_run(record)
    if record.status != RunStatus.done:
        sys.exit(1)


def _await_run(client: ConsoleClient, run_id: str) -> RunDetail:
    """Follow the run to its end, printing its rows as they land. The
    worker writes the run's row a moment after the start is accepted, so
    "no such run" is "not yet" within the grace, and a refusal after it."""
    born = time.monotonic()
    record: RunDetail | None = None

    def ended() -> bool:
        nonlocal record
        try:
            record = client.run(run_id)
        except ConsoleError as exc:
            if exc.status == 422 and time.monotonic() - born < START_GRACE_S:
                return False
            raise
        execution = record.execution.status
        if record.status != RunStatus.running or execution not in (
            EXECUTION_RUNNING,
            EXECUTION_NOT_FOUND,
        ):
            return True
        if execution == EXECUTION_NOT_FOUND and time.monotonic() - born >= START_GRACE_S:
            render.error(f"run {run_id} has no workflow after {START_GRACE_S:.0f}s")
            raise typer.Exit(1)
        return False

    follow_events(client, render.ERR.print, run_id=run_id, stop=ended)
    assert record is not None  # `ended` read it before the follow could stop
    return record


# -- the human's answer --------------------------------------------------------


@app.command(
    epilog=examples(
        'poseidon verdict auto-3d73e674 merge approve --reason "engine first, as the vision asks"',
        'poseidon verdict auto-3d73e674 budget reject --reason "not worth more spend"',
    )
)
def verdict(
    run_id: str = typer.Argument(..., help="The run parked on the gate."),
    gate: Gate = typer.Argument(..., help="The gate: merge, or budget."),
    verdict: Verdict = typer.Argument(..., help="approve or reject."),
    reason: str = REASON_OPTION,
) -> None:
    """Answer a gate a run parked on. The console records the verdict and
    your reason under your name, then delivers it. Approving a budget park
    lets the run continue under its cap as it stands."""
    approved = verdict is Verdict.approve
    result = _call(_client().resolve, run_id, gate.value, approved, reason)
    render.act(
        Text.assemble(
            "verdict recorded: ",
            f"{result.gate} ",
            render.status(GATE_APPROVED if result.approved else GATE_REJECTED),
            f" for run {result.run_id}, by {result.human}",
        )
    )


@app.command(epilog=examples("poseidon acts", "poseidon acts auto-3d73e674"))
def acts(
    run_id: str = typer.Argument(None, help="A run's id; omit for the company's acts."),
    as_json: bool = JSON_OPTION,
) -> None:
    """What you may do now, to the company or to one run: every act, with
    the arguments that decide it, whether it owes --reason, and why not
    when it would be refused. A preview: the act itself decides again,
    and is recorded."""
    listing = _call(_client().acts, run_id)
    if as_json:
        _json(listing)
        return
    render.table(
        ("act", "with", "reason", "allowed", "why not"),
        [
            (
                view.act,
                ", ".join(
                    f"{key}={value}"
                    for key, value in view.args.items()
                    if key != "run_id"
                ),
                "yes" if view.rationale_required else "",
                render.status("allow" if view.allowed else "deny"),
                ""
                if view.refusal is None
                else (view.refusal.rule or view.refusal.detail),
            )
            for view in listing.acts
        ],
        empty="no acts",
    )


# -- the window ------------------------------------------------------------------


@app.command(
    epilog=examples(
        "poseidon follow",
        "poseidon follow --run auto-3d73e674",
        "poseidon follow --after 1840",
    )
)
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
    sink = typer.echo if json_lines else render.OUT.print
    _call(lambda: follow_events(client, sink, after=after, run_id=run, raw=json_lines))


@app.command(epilog=examples("poseidon spend", "poseidon spend auto-3d73e674"))
def spend(
    run_id: str = typer.Argument(None, help="A run's id; omit for the company's."),
    since: str = typer.Option(
        None, help="Company-wide: only spend at or after this timestamp."
    ),
    as_json: bool = JSON_OPTION,
) -> None:
    """Spend to date: the company's, or one run's with RUN_ID — the
    total, and every model call behind it."""
    client = _client()
    if not run_id:
        total = _call(client.spend_since, since)
        if as_json:
            _json(total)
            return
        render.act(
            Text.assemble(
                f"spent {render.usd(total.usd)}",
                render.dim(
                    f"  ({total.prompt_tokens:,} prompt,"
                    f" {total.completion_tokens:,} completion tokens)"
                ),
            )
        )
        return
    result = _call(client.spend, run_id)
    if as_json:
        _json(result)
        return
    render.act(f"run {result.run_id}: {render.usd(result.usd)}")
    render.table(
        ("when", "who", "model", "tokens in / cached / out", "cost"),
        [
            (
                render.dim(render.clock(e.ts)),
                "/".join(p for p in (e.role, e.agent_id) if p),
                e.model,
                f"{e.prompt_tokens:,} / {e.cached_tokens:,} / {e.completion_tokens:,}",
                render.usd(e.usd),
            )
            for e in result.entries
        ],
        empty="no model calls",
    )


if __name__ == "__main__":
    app()
