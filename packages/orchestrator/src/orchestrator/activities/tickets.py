"""The ticket lineage of a delivered pull request, and the platform act
that closes it once the pull request merges.

A pull request fulfils the ticket its implement run consumed — the run's
task names it (`implement issue #N`). A rework ticket carries its chain in
its canonical title (`Rework <depth> for PR #<root>`), so the root PR, the
root ticket and every rework filed for the chain are readable back by the
platform without trusting any agent. `rework_depth` reads the depth from
here; `close_fulfilled_tickets` closes the chain.

The close is the merge's consequence, never a role's act: the platform
identity calls `issues.close` with a rationale, and the gateway's audit
row is the record. A pull request closed without merging closes nothing —
the work did not land, and the ticket is the human's.
"""

import asyncio
import re

from common.audit import RATIONALE_LIMIT
from common.tasks import ISSUE_TASK_RE
from common.telemetry import get_logger
from orchestrator.clients import GatewayClient
from registry import Runs

# The canonical rework-ticket title. The chain depth and the root PR ride
# the title so the platform can read the bound back without trusting any
# agent: `rework_depth` parses it from the ticket the failing PR's own
# implement run consumed, under the platform identity. Forging a higher
# depth into a hand-filed ticket only escalates to the human sooner — the
# parse can shrink autonomy, never extend it.
REWORK_TITLE_RE = re.compile(r"^Rework (\d+) for PR #(\d+)")

_log = get_logger("orchestrator.tickets")


def rework_title(attempt: int, root_pr: int, pr_title: str) -> str:
    return f"Rework {attempt} for PR #{root_pr}: {pr_title[:80]}"


def merged_outside_rationale(pr_number: int) -> str:
    """Why the platform closes the tickets of a PR a human merged by hand —
    the heartbeat's observation and the review's PR read say it alike."""
    return f"pull request #{pr_number} merged outside the platform"


def consumed_ticket(runs: Runs, company: str, pr_number: int) -> int | None:
    """The ticket the implement run that delivered `pr_number` consumed,
    or None for a PR no implement run delivered or one delivered from a
    free-form task."""
    implement = runs.implement_run_for_pr(company, pr_number)
    if implement is None:
        return None
    match = ISSUE_TASK_RE.match(implement.task or "")
    return int(match.group(1)) if match else None


async def close_fulfilled_tickets(
    gateway: GatewayClient,
    runs: Runs,
    company: str,
    run_id: str,
    repo_url: str,
    pr_number: int,
    rationale: str,
) -> list[int]:
    """Close the tickets a merged `pr_number` fulfils: the one its implement
    run consumed and, when that is a rework ticket, the chain's root ticket
    and every open rework ticket filed for the same root PR — a merged
    chain leaves nothing open. Each is read first and an already-closed one
    skipped, so a repeated close is harmless. Returns the numbers closed."""
    ticket = await asyncio.to_thread(consumed_ticket, runs, company, pr_number)
    if ticket is None:
        return []
    consumed = await gateway.read_issue(run_id, repo_url, ticket)
    chain = {ticket: consumed}
    match = REWORK_TITLE_RE.match(str(consumed.get("title") or ""))
    if match:
        root_pr = int(match.group(2))
        root = await asyncio.to_thread(consumed_ticket, runs, company, root_pr)
        if root is not None and root not in chain:
            chain[root] = await gateway.read_issue(run_id, repo_url, root)
        for issue in await gateway.read_open_issues(run_id, repo_url):
            rework = REWORK_TITLE_RE.match(str(issue.get("title") or ""))
            if rework and int(rework.group(2)) == root_pr:
                chain.setdefault(int(issue["number"]), issue)
    closed = []
    for number, issue in sorted(chain.items()):
        if issue.get("state") != "open":
            continue
        await gateway.close_issue(run_id, repo_url, number, rationale[:RATIONALE_LIMIT])
        closed.append(number)
    _log.bind(run_id=run_id).info(
        "tickets closed", extra={"pr_number": pr_number, "closed": closed}
    )
    return closed
