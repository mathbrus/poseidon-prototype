"""How the CLI speaks.

The console's answer is the truth and stays reachable verbatim (`--json`
on every read); the default is a rendering a person reads at a glance.
Four shapes and nothing else: a *record* (label left, value right), a
*table* (a header row, one row per item), an *act* (one sentence saying
what was recorded and under whose name) and an *error* (one line on
stderr, the rule dim beneath). Colour lives in status words alone —
running blue, done green, failed red, parked yellow — plus dim ids and
timestamps; no boxes, no panels, no spinners. `rich` turns colour off by
itself when stdout is not a terminal or `NO_COLOR` is set.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from common.console import EXECUTION_NOT_FOUND, EXECUTION_RUNNING, AuditEventView
from common.workflows import (
    GATE_APPROVED,
    GATE_ESCALATED,
    GATE_EXPIRED,
    GATE_HALTED,
    GATE_PARKED,
    GATE_REJECTED,
    HEARTBEAT_SLEEPING,
    HEARTBEAT_TICKING,
    HEARTBEAT_WORKFLOW,
    PARKED_PREFIX,
    RUN_FINALIZING,
    RUN_STATUS_DONE,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    TICK_IDLE,
    TICK_PAUSED,
    TICK_REFUSED,
    TICK_STARTED,
    TICK_STOPPED,
    workflow_id,
)

OUT = Console(highlight=False, soft_wrap=True)
ERR = Console(stderr=True, highlight=False, soft_wrap=True)

# The one vocabulary of colour: a status word carries it, nothing else does.
STATUS_STYLE = {
    # A run's status and its workflow's state share the word `running`,
    # and the heartbeat's `paused` state is its tick's outcome word.
    RUN_STATUS_RUNNING: "blue",
    RUN_FINALIZING: "blue",
    EXECUTION_RUNNING: "blue",
    HEARTBEAT_TICKING: "blue",
    TICK_STARTED: "blue",
    "activating": "yellow",
    TICK_PAUSED: "yellow",
    GATE_PARKED: "yellow",
    GATE_ESCALATED: "yellow",
    GATE_HALTED: "yellow",
    GATE_EXPIRED: "yellow",
    RUN_STATUS_DONE: "green",
    "COMPLETED": "green",
    "converged": "green",
    GATE_APPROVED: "green",
    "approve": "green",
    "allow": "green",
    "pass": "green",
    "ok": "green",
    RUN_STATUS_FAILED: "red",
    "FAILED": "red",
    "deny": "red",
    GATE_REJECTED: "red",
    "reject": "red",
    "fail": "red",
    "error": "red",
    "cancelled": "red",
    "CANCELED": "red",
    "TERMINATED": "red",
    TICK_REFUSED: "red",
    TICK_STOPPED: "red",
    EXECUTION_NOT_FOUND: "dim",
    TICK_IDLE: "dim",
    HEARTBEAT_SLEEPING: "dim",
}
DIM = "dim"


def status(word: Any) -> Text:
    """A status word in its colour; an unknown word in none."""
    text = str(word or "")
    style = STATUS_STYLE.get(text) or ("yellow" if text.startswith(PARKED_PREFIX) else "")
    return Text(text, style=style)


def dim(value: Any) -> Text:
    return Text(str(value), style=DIM)


# -- time and money --------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """A registry timestamp (ISO 8601, with offset) as an aware datetime;
    None for nothing or for something that is not a timestamp."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def duration(seconds: float | None) -> str:
    """`38 s`, `4 min 02 s`, `2 h 07 min`, `3 d 4 h` — the largest two units."""
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def ago(value: Any, now: datetime | None = None) -> str:
    """`just now`, `12 min ago`, `4 h ago`, `3 d ago`; the value as given
    when it is not a timestamp."""
    then = parse_ts(value)
    if then is None:
        return str(value or "")
    now = now or datetime.now(UTC)
    seconds = (now - then).total_seconds()
    if seconds < 0:
        return f"in {duration(-seconds)}"
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def clock(value: Any) -> str:
    """`12:04:10` — the time of day a row landed, for a story's left edge."""
    then = parse_ts(value)
    return then.strftime("%H:%M:%S") if then else "--:--:--"


def usd(amount: Any) -> str:
    """`$4.20`, never `4.2000001`; `$0.0031` keeps the cents of a small call."""
    try:
        value = float(amount or 0.0)
    except (TypeError, ValueError):
        return str(amount)
    if 0 < value < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


# -- the shapes --------------------------------------------------------------------


def record(pairs: Iterable[tuple[str, Any]], console: Console = OUT) -> None:
    """A two-column block: the label dim on the left, the value on the
    right. A None value drops its row; the caller decides what a person
    needs to see."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM, no_wrap=True)
    grid.add_column()
    for label, value in pairs:
        if value is None or value == "":
            continue
        grid.add_row(label, value if isinstance(value, Text) else str(value))
    console.print(grid)


# Columns a table never wraps: an id or a name broken across two lines
# cannot be copied or grepped.
UNWRAPPED = frozenset({"run", "who", "model", "when", "workflow", "status", "outcome"})


def table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    console: Console = OUT,
    *,
    empty: str = "nothing yet",
) -> None:
    """A header row and one row per item; a rule under the header, no
    box. Says `empty` when there are no rows rather than printing a bare
    header."""
    rows = list(rows)
    if not rows:
        console.print(Text(empty, style=DIM))
        return
    grid = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style=DIM)
    for name in columns:
        grid.add_column(name, overflow="fold", no_wrap=name in UNWRAPPED)
    for values in rows:
        grid.add_row(
            *(
                v if isinstance(v, Text) else str(v if v is not None else "")
                for v in values
            )
        )
    console.print(grid)


def act(sentence: str | Text, console: Console = OUT) -> None:
    """One sentence: what was recorded, under whose name."""
    console.print(sentence)


def error(message: str, rule: str | None = None, console: Console = ERR) -> None:
    """What was refused; the rule, when there is one, dim on its own line."""
    console.print(message)
    if rule:
        console.print(Text(f"rule {rule}", style=DIM))


def diff(text: str, console: Console = OUT) -> None:
    """A blueprint diff as the console words it: added lines green, removed
    lines red, the rest as they are."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("+ ") or stripped == "+":
            console.print(Text(line, style="green"))
        elif stripped.startswith("- ") or stripped == "-":
            console.print(Text(line, style="red"))
        else:
            console.print(Text(line))


# -- an audit row as a line ---------------------------------------------------------

HEARTBEAT_PREFIX = workflow_id(HEARTBEAT_WORKFLOW, "")
RULE_WIDTH = 96


def row(event: AuditEventView) -> Text:
    """One audit row, one line: the time, the run (or `heartbeat`), who
    acted, the tool, the decision in its colour, and the rule when it
    names why — a denial, or a tick that ended on one. Rows carry no
    arguments, so a line names the tool and no more — except the actor's
    stated rationale, which follows on its own line. The same line
    `follow`, `run story` and `runs` print."""
    run_id = event.run_id
    on_heartbeat = run_id.startswith(HEARTBEAT_PREFIX)
    where = "heartbeat" if on_heartbeat else (run_id or "-")
    who = "/".join(part for part in (event.role, event.agent_id) if part) or "-"
    rule = event.rule or ""
    line = Text.assemble(
        dim(clock(event.ts)),
        "  ",
        f"{where:<28}",
        "  ",
        f"{who:<26}",
        "  ",
        event.tool,
        "  ",
        status(event.decision),
    )
    if rule and (event.decision == "deny" or on_heartbeat):
        # A refused tick's rule can carry a whole error message; the line
        # names it, the row (`--json`) has all of it.
        short = rule if len(rule) <= RULE_WIDTH else rule[: RULE_WIDTH - 1] + "…"
        line.append(f" ({short})", style=DIM)
    rationale = (event.rationale or "").strip()
    if rationale:
        line.append("\n" + " " * 10 + rationale)
    return line
