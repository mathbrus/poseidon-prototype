"""The task texts of the runs the heartbeat starts, and the regexes that
read them back.

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

# The run row's task for a scope run — a label for humans reading the runs
# table; nothing parses it.
SCOPE_TASK = "scope the next slice"

# The crew a ticket about the company's documents routes to — the one the
# platform names in the trailer of every bootstrap ticket and every
# architecture-request ticket it files, and the catalog's crew of the
# steward. Named once, here, because the gateway files those tickets and
# the catalog defines the crew.
DOCUMENT_CREW = "stewardship"


def issue_run_task(number: int) -> str:
    """The run row's task for an issue-driven implement run."""
    return f"implement issue #{number}"


def review_run_task(number: int) -> str:
    """The run row's task for a review run."""
    return f"review pull request #{number}"
