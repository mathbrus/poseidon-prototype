"""The task texts of heartbeat-startable work, and the regexes that read
them back.

A run on an issue or a pull request carries the same text as every other
run on that item — the number is the work item's key. The registry's
live-run indexes and attempt aggregates group by it, and the orchestrator
composes it, so both sides import the grammar from here rather than
spelling the sentence twice. `registry/schema.sql` repeats the prefixes as
SQL `LIKE` patterns; keep them equal to these.
"""

import re

ISSUE_TASK_RE = re.compile(r"^implement issue #(\d+)$")
REVIEW_TASK_RE = re.compile(r"^review pull request #(\d+)$")


def issue_run_task(number: int) -> str:
    """The run row's task for an issue-driven implement run."""
    return f"implement issue #{number}"


def review_run_task(number: int) -> str:
    """The run row's task for a review run."""
    return f"review pull request #{number}"
