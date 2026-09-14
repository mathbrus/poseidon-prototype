"""`poseidon follow`: the company's audit ledger as it lands.

A cursor poll over the ledger, not a stream: the serial id is a lossless
cursor, so a follower never repeats or skips a row, and an interrupted
follow resumes from the last id it printed. Spend rides along from the
budget ledger and is said only when it moves. The rows themselves are
rendered by `render.row`, the one line shape `runs`, `run story` and
this share.
"""

import time
from collections.abc import Callable
from typing import Any

from rich.text import Text

from poseidon_cli.client import ConsoleClient
from poseidon_cli.render import row, usd

POLL_S = 2.0


def spend_line(spend: dict[str, Any], previous: float | None) -> Text | None:
    """A line when the total moved since the last poll, else nothing."""
    total = float(spend.get("usd", 0.0))
    if previous is not None and abs(total - previous) < 1e-9:
        return None
    delta = "" if previous is None else f"  (+{usd(total - previous)})"
    total_part = f"{usd(total)} total{delta}"
    run = spend.get("run_usd")
    if run is None:
        return Text(f"spend   {total_part}", style="dim")
    return Text(f"spend   run {usd(run)} of {total_part}", style="dim")


def follow(
    client: ConsoleClient,
    sink: Callable[[Any], None],
    *,
    after: int = 0,
    run_id: str | None = None,
    raw: bool = False,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Print the company's rows as they land until `stop()` says so (or
    the user interrupts). Returns the last id seen, the cursor to resume
    from with `--after`. `raw` hands the sink each row as stored, as one
    JSON object per line, and nothing else."""
    import json

    cursor = after
    total: float | None = None
    started = time.monotonic()
    try:
        while True:
            page = client.events(after=cursor, run_id=run_id)
            for event in page["events"]:
                sink(json.dumps(event) if raw else row(event))
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
            Text(
                f"followed for {elapsed}s; spend {usd(total or 0.0)} total;"
                f" resume with --after {cursor}",
                style="dim",
            )
        )
    return cursor
