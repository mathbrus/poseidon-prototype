"""`poseidon follow` — watching the company from the terminal.

The audit ledger is the company's one journal: every tick, run start,
dispatch, tool call, decision, gate park and verdict is a row in it, and
its serial id is a cursor that repeats nothing and skips nothing. A
follower polls the console for the rows above the last id it saw and
prints each as one line; spend, metered into the budget ledger by the
LLM gateway, rides along and is printed only when it moves.
"""

import time
from collections.abc import Callable
from typing import Any

from poseidon_cli.client import ConsoleClient

POLL_S = 2.0
RULE_WIDTH = 96

# Rows the heartbeat journals carry its shared run id; a reader wants the
# word, not the id.
HEARTBEAT_PREFIX = "heartbeat-"


def render(row: dict[str, Any]) -> str:
    """One row, one line: the time, the run (or `heartbeat`), who acted,
    the tool, the decision, and the rule when it names why — a denial,
    or a tick that ended on one. Rows carry a digest of the arguments,
    never the arguments, so a line names the tool and no more."""
    when = str(row.get("ts", ""))[11:19] or "--:--:--"
    run_id = str(row.get("run_id", ""))
    where = "heartbeat" if run_id.startswith(HEARTBEAT_PREFIX) else (run_id or "-")
    who = "/".join(part for part in (row.get("role"), row.get("agent_id")) if part) or "-"
    decision = str(row.get("decision", ""))
    rule = str(row.get("rule") or "")
    verdict = decision
    if rule and (decision == "deny" or run_id.startswith(HEARTBEAT_PREFIX)):
        # A refused tick's rule can carry a whole error message; the line
        # names it, the row (`--json`) has all of it.
        short = rule if len(rule) <= RULE_WIDTH else rule[: RULE_WIDTH - 1] + "…"
        verdict = f"{decision} ({short})"
    return f"{when}  {where:<28}  {who:<26}  {row.get('tool', '')}  {verdict}"


def spend_line(spend: dict[str, Any], previous: float | None) -> str | None:
    """A line when the total moved since the last poll, else nothing."""
    total = float(spend.get("usd", 0.0))
    if previous is not None and abs(total - previous) < 1e-9:
        return None
    delta = "" if previous is None else f"  (+${total - previous:.2f})"
    total_part = f"${total:.2f} total{delta}"
    run = spend.get("run_usd")
    if run is None:
        return f"spend   {total_part}"
    return f"spend   run ${float(run):.2f} of {total_part}"


def follow(
    client: ConsoleClient,
    sink: Callable[[str], None],
    *,
    after: int = 0,
    run_id: str | None = None,
    raw: bool = False,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Print the company's rows as they land until `stop()` says so (or
    the user interrupts). Returns the last id seen, the cursor to resume
    from with `--after`."""
    import json

    cursor = after
    total: float | None = None
    started = time.monotonic()
    try:
        while True:
            page = client.events(after=cursor, run_id=run_id)
            for row in page["events"]:
                sink(json.dumps(row) if raw else render(row))
            cursor = int(page["next"])
            line = spend_line(page["spend"], total)
            if line and not raw:
                sink(line)
            total = float(page["spend"].get("usd", 0.0))
            if stop is not None and stop():
                break
            sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    elapsed = int(time.monotonic() - started)
    if not raw:
        sink(
            f"followed for {elapsed}s; spend ${total or 0.0:.2f} total; resume with --after {cursor}"
        )
    return cursor
