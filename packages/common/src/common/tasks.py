"""The task texts of the runs the heartbeat starts, the regexes that
read them back, and the trailer a ticket names its crew with.

A run on an issue or a pull request carries the same text as every other
run on that item — the number is the work item's key. The registry's
live-run indexes and attempt aggregates group by it, and the orchestrator
composes it, so both sides import the grammar from here rather than
spelling the sentence twice. `registry/schema.sql` repeats the prefixes as
SQL `LIKE` patterns; keep them equal to these. A scope run works no item:
its task is a fixed label.
"""

import re

ISSUE_TASK_RE = re.compile(r"^implement issue #(\d+)$")
REVIEW_TASK_RE = re.compile(r"^review pull request #(\d+)$")

# The run row's task for a scope run. Every scope run works the same
# text, so a lookup by task finds the latest scope run; nothing parses a
# number out of it.
SCOPE_TASK = "scope the next slice"

# A ticket names the crew that should fulfil it with a trailer line
# (`Crew: contract-authoring`); without one, the workflow's configured
# crew fulfils it. The PM, the platform's bootstrap filing and the
# gateway's architecture-request filing all write the trailer, and the
# implement workflow reads it — one grammar, here. The name grammar is
# the blueprint's `_NAME`.
TICKET_CREW_RE = re.compile(r"^Crew:\s*([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE)


def issue_run_task(number: int) -> str:
    """The run row's task for an issue-driven implement run."""
    return f"implement issue #{number}"


def review_run_task(number: int) -> str:
    """The run row's task for a review run."""
    return f"review pull request #{number}"


def crew_trailer(crew: str) -> str:
    """The trailer line that routes a ticket to `crew`."""
    return f"Crew: {crew}"


def ticket_crew(text: str) -> str | None:
    """The crew a ticket names, parsed deterministically. The *last* match
    wins: the trailer is written by whoever filed the ticket, and text
    quoted above it is untrusted input — a poisoned document quoted into
    a body must not outrank the filer's own trailer. Whether the named
    crew may be spawned is policy's to decide at fulfilment."""
    matches = TICKET_CREW_RE.findall(text)
    return matches[-1] if matches else None
