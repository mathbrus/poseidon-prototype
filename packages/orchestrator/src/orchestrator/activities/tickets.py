"""The ticket lineage of a delivered pull request, and the platform acts
that end it: the close once the pull request merges, the refusal when it
will not.

A pull request fulfils the ticket its implement run consumed — the run's
task names it (`implement issue #N`). A rework ticket carries its chain in
its canonical title (`Rework <depth> for PR #<root>`), so the root PR, the
root ticket and every rework filed for the chain are readable back by the
platform without trusting any agent. `rework_depth` reads the depth from
here; `close_fulfilled_tickets` closes the chain.

Every act is the platform's, never a role's: the platform identity calls
`issues.close`, `git.close_pr` and `issues.comment` with its reason, and
the gateway's audit row is the record. A pull request closed without
merging is refused (`refuse_delivery`): the work did not land, so its
ticket goes back on the backlog as one spent attempt, with the reason on
it for the next attempt to read.
"""

import asyncio
import re

from common.audit import RATIONALE_LIMIT
from common.tasks import CLOSE_COMPLETED, ISSUE_TASK_RE
from common.telemetry import get_logger
from common.workflows import PR_MERGED, PR_REFUSED
from orchestrator.clients import GatewayClient
from registry import Runs

# The canonical rework-ticket title. The chain depth and the root PR ride
# the title so the platform can read the bound back without trusting any
# agent: `rework_depth` parses it from the ticket the failing PR's own
# implement run consumed, under the platform identity. Forging a higher
# depth into a hand-filed ticket only escalates to the human sooner — the
# parse can shrink autonomy, never extend it.
REWORK_TITLE_RE = re.compile(r"^Rework (\d+) for PR #(\d+)")

# Why a delivery the git host shows closed without merging is refused:
# nobody inside the platform closed it.
CLOSED_OUTSIDE = "closed outside the platform"

_log = get_logger("orchestrator.tickets")


def rework_title(attempt: int, root_pr: int, pr_title: str) -> str:
    return f"Rework {attempt} for PR #{root_pr}: {pr_title[:80]}"


def merged_outside_rationale(pr_number: int) -> str:
    """Why the platform closes the tickets of a PR a human merged by hand —
    the heartbeat's observation and the review's PR read say it alike."""
    return f"pull request #{pr_number} merged outside the platform"


def refused_comment(pr_number: int, reason: str, attempt: int, max_attempts: int) -> str:
    """What a ticket says when its delivery is refused: which pull
    request, why, and where the ticket stands."""
    head = f"Pull request #{pr_number} was not merged: {reason}"
    if attempt < max_attempts:
        return (
            f"{head}\n\nThe ticket is back on the backlog: attempt {attempt} of"
            f" {max_attempts}."
        )
    return (
        f"{head}\n\nThat was attempt {attempt} of {max_attempts}, the last the"
        " blueprint's `heartbeat.max_attempts` allows."
    )


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
    fulfilled = await gateway.read_issue(run_id, repo_url, ticket)
    chain = {ticket: fulfilled}
    match = REWORK_TITLE_RE.match(str(fulfilled.get("title") or ""))
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
        await gateway.close_issue(
            run_id, repo_url, number, CLOSE_COMPLETED, rationale[:RATIONALE_LIMIT]
        )
        closed.append(number)
    _log.bind(run_id=run_id).info(
        "tickets closed", extra={"pr_number": pr_number, "closed": closed}
    )
    return closed


async def refuse_delivery(
    gateway: GatewayClient,
    runs: Runs,
    company: str,
    run_id: str,
    repo_url: str,
    pr_number: int,
    reason: str,
    max_attempts: int,
) -> int | None:
    """Refuse a delivered pull request: close it with the reason posted on
    it, remember the refusal on the run that delivered it, and tell the
    ticket that run consumed why it is back. The refusal gives the ticket
    back as one spent attempt (`Runs.issue_attempts`), and the next
    attempt's brief carries the reason (`Runs.refusals`). Each act is
    idempotent but the comment, which a retry can repeat. Returns the
    ticket's number, or None when the delivery worked no open ticket."""
    reason = reason[:RATIONALE_LIMIT]
    await gateway.close_pr(run_id, repo_url, pr_number, reason)
    await asyncio.to_thread(runs.mark_pr_settled, company, pr_number, PR_REFUSED, reason)
    ticket = await asyncio.to_thread(consumed_ticket, runs, company, pr_number)
    if ticket is None:
        return None
    issue = await gateway.read_issue(run_id, repo_url, ticket)
    if issue.get("state") != "open":
        return None
    attempts = (await asyncio.to_thread(runs.issue_attempts, company)).get(ticket)
    attempt = attempts.count if attempts else 1
    await gateway.comment_issue(
        run_id,
        repo_url,
        ticket,
        refused_comment(pr_number, reason, attempt, max_attempts),
    )
    _log.bind(run_id=run_id).info(
        "delivery refused",
        extra={"pr_number": pr_number, "ticket": ticket, "attempt": attempt},
    )
    return ticket


async def settle_delivery(
    gateway: GatewayClient,
    runs: Runs,
    company: str,
    run_id: str,
    repo_url: str,
    pr_number: int,
    state: str,
    max_attempts: int,
) -> None:
    """A delivered pull request the git host shows settled by someone
    outside the platform, learned once: merged by hand, the tickets it
    fulfils close; closed by hand, it is refused like any delivery the
    platform refuses. Acts first, remembers after, so a failure leaves the
    pull request to be read — and settled — again. A pull request the
    company did not deliver, or one already settled, is left alone."""
    delivering = await asyncio.to_thread(runs.implement_run_for_pr, company, pr_number)
    if delivering is None or delivering.pr_settled is not None:
        return
    if state == PR_MERGED:
        await close_fulfilled_tickets(
            gateway,
            runs,
            company,
            run_id,
            repo_url,
            pr_number,
            merged_outside_rationale(pr_number),
        )
        await asyncio.to_thread(runs.mark_pr_settled, company, pr_number, PR_MERGED)
        return
    await refuse_delivery(
        gateway, runs, company, run_id, repo_url, pr_number, CLOSED_OUTSIDE, max_attempts
    )
