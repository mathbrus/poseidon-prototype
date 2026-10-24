"""The review activities: thin clients, as always —
sequencing, the merge gate and its timers live in the workflow.

Retry safety, per activity:
    read_pr             — read-only against GitHub via the gateway; safe.
                          A merged or closed PR is refused here with the
                          named rule `pr_not_open` (non-retryable, audited)
                          — nothing downstream can succeed on it, and the
                          ledger should not pay for a run that can only
                          fail at the head ref. A delivered PR first learned
                          settled here is settled like the observation
                          settles it (`settle_delivery`: idempotent but a
                          refusal's comment on its ticket).
    record_verdict      — appends the reviewer's journaled verdict (same
                          append-only posture) and sets it on the run row,
                          an idempotent update.
    reviewer_independent— read-only against the registry; safe.
    read_human_verdict  — read-only against the audit store; safe.
    pr_tips             — read-only against GitHub via the gateway, but a
                          PR found settled is settled like `read_pr`
                          settles it; safe the same way.
    resolve_merge       — re-evaluates the compiled merge.resolve decision
                          and re-audits; the honest record of what was
                          asked (the heartbeat_authorize posture).
    rework_depth        — read-only (registry + one gateway issue read);
                          safe.
    file_rework         — creates an issue; not idempotent, so a retry can
                          file a duplicate ticket — a noisy backlog, never
                          a lost finding.
    close_superseded_pr — closes the failed PR once its rework ticket is
                          filed and marks it superseded; closing a closed
                          PR adds nothing, so safe. Its own activity, so a
                          retried close never files the ticket twice.
    refuse_delivery     — closes the PR, marks it refused and comments on
                          its ticket; a retry can repeat only the comment.
    close_tickets       — closes the tickets the merged PR fulfils, each
                          read first and skipped when already closed; safe.
    merge_pr            — GitHub's merge endpoint is idempotent for an
                          already-merged PR (it answers 405, surfaced as a
                          backend error the workflow reports honestly); a
                          head that moved answers the host's refusal.

The merge itself: `git.merge_pr` is a *gateway tool
call* under the orchestrator's platform identity — one of the two mediation
paths, audited at the gateway like any agent call — issued by the workflow
only after `merge.resolve` allowed. On the human path that decision reads
the verdict the console recorded under the person's name (the audit store
is the artifact; `read_human_verdict` fetches it), so a signal that
reached the workflow by any other road releases nothing. The gate outcome
events are the `process-gate` audit source; the resolve is the
`process-lifecycle` one; together the story shows who released the merge
and that policy, not a bug, let it through.
"""

import asyncio
import time
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.audit import (
    RATIONALE_LIMIT,
    REVIEW_CONTRACT_EDIT,
    REVIEW_READ_PR,
    REVIEW_VERDICT,
    args_digest,
)
from common.identity import LIFECYCLE_PLATFORM, ORCHESTRATOR_ID, PLATFORM_ROLE
from common.opa import evaluate_decision
from common.policy import MERGE_RESOLVE
from common.telemetry import get_logger
from common.workflows import MERGE_GATE, PR_SUPERSEDED, workflow_id
from orchestrator.activities.blueprint import company_repo
from orchestrator.activities.company import applied_company
from orchestrator.activities.tickets import (
    REWORK_TITLE_RE,
    close_fulfilled_tickets,
    consumed_ticket,
    refuse_delivery,
    rework_title,
    settle_delivery,
)
from orchestrator.audit import record_event
from orchestrator.clients import GatewayClient, pr_settled
from orchestrator.config import WorkerSettings
from registry import AuditStore, Companies, RunParticipants, Runs

# The named rule a review run fails with when its PR is already merged or
# closed — the run record's error starts with it, and the audit row
# carries it, the way `budget_exhausted` names the other birth refusal.
PR_NOT_OPEN = "pr_not_open"


def contract_deletions(
    files: list[dict[str, Any]], contract_paths: list[str]
) -> dict[str, int] | None:
    """The model-free deletion facts for a diff that touches the contract
    tree: test functions and assertions removed/added, counted straight off
    the patch text — plain string counting, no model, so adversarial prose
    cannot rewrite it. None when the diff leaves the contract tree alone."""
    counts = {
        "removed_tests": 0,
        "added_tests": 0,
        "removed_asserts": 0,
        "added_asserts": 0,
    }
    touched = False
    for f in files:
        path = f.get("path") or ""
        if not any(path.startswith(prefix) for prefix in contract_paths):
            continue
        touched = True
        for line in (f.get("patch") or "").splitlines():
            if line.startswith("-") and not line.startswith("---"):
                kind = "removed"
            elif line.startswith("+") and not line.startswith("+++"):
                kind = "added"
            else:
                continue
            content = line[1:].lstrip()
            if content.startswith("def test_") or content.startswith("async def test_"):
                counts[f"{kind}_tests"] += 1
            if content.startswith("assert ") or content == "assert":
                counts[f"{kind}_asserts"] += 1
    return counts if touched else None


def contract_edit_comment(deletions: dict[str, int]) -> str:
    """The human-facing warning, posted on the PR by the platform: the
    merge signal's reader sees what the oracle edit removes before they
    approve it."""
    return (
        "**Contract tree edited.** This pull request changes the "
        "contract/acceptance tests automated merges are graded by, so it "
        "is never an automated merge — it parks on the human `merge` "
        "signal.\n\n"
        f"- test functions: −{deletions['removed_tests']} "
        f"/ +{deletions['added_tests']}\n"
        f"- assertions: −{deletions['removed_asserts']} "
        f"/ +{deletions['added_asserts']}\n\n"
        "Counted by the platform from the diff. A net loss of tests or "
        "assertions weakens the spec every future automated merge is "
        "graded by — approve deliberately."
    )


def rework_body(
    pr_number: int,
    pr_url: str,
    reason: str,
    findings: str,
    paths: list[str],
    attempt: int,
    review_run_id: str,
) -> str:
    """The ticket is the rework implementer's whole brief — the implement
    crew reads tickets, not PRs — so it carries the failure, the findings,
    and the changed surface."""
    changed = "\n".join(f"- {p}" for p in paths) or "(none recorded)"
    return (
        f"Pull request #{pr_number} ({pr_url}) failed its automated"
        f" review: {reason}.\n\n"
        f"Reviewer findings: {findings}\n\n"
        f"Changed paths under review:\n{changed}\n\n"
        "Fix the findings and deliver a new pull request with tests."
        " Acceptance: a review of the new pull request passes.\n\n"
        f"Filed by the platform for review run {review_run_id}"
        f" (rework attempt {attempt})."
    )


class ReviewActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        audit: AuditStore,
        participants: RunParticipants,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._audit = audit
        self._participants = participants
        self._log = get_logger("orchestrator.review")

    async def _repo(self) -> dict[str, str]:
        """The company's repository, from its applied blueprint."""
        return await asyncio.to_thread(company_repo, self._companies, self._company())

    def _company(self) -> str:
        return applied_company(self._companies)

    def _max_attempts(self) -> int:
        """The blueprint's attempt cap on a work item."""
        spec = self._companies.get(self._company()) or {}
        return int(spec["heartbeat"]["max_attempts"])

    @activity.defn(name="read_pr")
    async def read_pr(
        self, run_id: str, pr_number: int, contract_paths: list[str] | None = None
    ) -> dict[str, Any]:
        """PR metadata under the platform identity: the head commit is
        what every review sandbox is provisioned from and what the merge
        names, the title and URL seed the reviewer's task text and the run
        record.

        The PR must still be open. The heartbeat offers only open PRs, but
        the world moves between its observation and this read — and a
        human's `poseidon start review` can name any number — so the
        state is the git host's word at the moment of the read. A settled
        PR is a refusal with a named rule, journaled in the run's story,
        never a crash further down at a branch that no longer exists."""
        company = self._company()
        repo_url = (await self._repo())["url"]
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            pr = await gateway.read_pr(run_id, repo_url, pr_number)
            settled = pr_settled(pr)
            if settled:
                # Settled outside the platform and perhaps first learned
                # here: settled like the observation settles it, so no
                # later observation reads this PR again.
                await settle_delivery(
                    gateway,
                    self._runs,
                    company,
                    run_id,
                    repo_url,
                    pr_number,
                    settled,
                    await asyncio.to_thread(self._max_attempts),
                )
        if settled:
            # The git host's word, merged or closed, whatever became of
            # the delivery.
            state = "merged" if pr.get("merged") else "closed"
            await self._append_event(
                run_id=run_id,
                tool=REVIEW_READ_PR,
                payload={"number": pr_number, "state": state},
                decision="deny",
                rule=PR_NOT_OPEN,
                source="process-lifecycle",
                workflow_id=workflow_id("review", run_id),
            )
            raise ApplicationError(
                f"{PR_NOT_OPEN}: pull request #{pr_number} is {state}"
                " — nothing to review",
                non_retryable=True,
            )
        if not pr.get("head_sha"):
            raise RuntimeError(
                f"pull request #{pr_number} has no head commit — nothing to"
                " provision a review sandbox from"
            )
        # The reviewer reads the diff itself (git.read is in its scope);
        # the workflow only needs the shape, not the patches — plus the
        # changed paths, which the merge-time class check resolves against,
        # and the model-free deletion facts when the diff edits the
        # contract tree.
        shaped = {k: v for k, v in pr.items() if k != "files"}
        shaped["paths"] = sorted(
            {f["path"] for f in pr.get("files") or [] if f.get("path")}
        )
        shaped["contract_deletions"] = contract_deletions(
            pr.get("files") or [], contract_paths or []
        )
        return shaped

    @activity.defn(name="pr_tips")
    async def pr_tips(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """The pull request's head and its base branch's tip as they stand
        now — what the one merge site compares with the head the run
        pinned and the base it was reviewed against. A PR someone merged
        or closed while the run waited is settled here as `read_pr`
        settles it, and answered `settled`."""
        company = self._company()
        repo_url = (await self._repo())["url"]
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            pr = await gateway.read_pr(run_id, repo_url, pr_number)
            settled = pr_settled(pr)
            if settled:
                await settle_delivery(
                    gateway,
                    self._runs,
                    company,
                    run_id,
                    repo_url,
                    pr_number,
                    settled,
                    await asyncio.to_thread(self._max_attempts),
                )
        return {
            "head_sha": pr.get("head_sha") or "",
            "base_sha": pr.get("base_sha") or "",
            "settled": "merged" if pr.get("merged") else "closed" if settled else None,
        }

    @activity.defn(name="contract_tree")
    async def contract_tree(self, run_id: str, contract_paths: list[str]) -> list[str]:
        """The declared contract paths the base ref actually holds — the
        tree the composed verify replays. Read by the platform from the
        git host, never from a sandbox: a company that has not written its
        contract yet has nothing to replay, and the review must know that
        from the world, not from a run that collected nothing."""
        repo = await self._repo()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            present = await gateway.present_paths(
                run_id, repo["url"], repo["base_ref"], contract_paths
            )
        self._log.bind(run_id=run_id).info(
            "contract tree", extra={"declared": contract_paths, "present": present}
        )
        return present

    async def _append_event(
        self,
        *,
        run_id: str,
        tool: str,
        payload: dict[str, Any],
        decision: str,
        rule: str,
        source: str,
        workflow_id: str,
        rationale: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        """Append-only, so a retried append duplicates a row, never loses
        one."""
        await record_event(
            self._audit,
            run_id=run_id,
            tool=tool,
            digest=args_digest(payload),
            decision=decision,
            rule=rule,
            rationale=rationale,
            source=source,
            company=self._company(),
            workflow=workflow_id,
            latency_ms=latency_ms,
        )

    @activity.defn(name="record_verdict")
    async def record_verdict(
        self, run_id: str, verdict: str, rationale: str, workflow_id: str
    ) -> None:
        """One `source=process-lifecycle` audit event journaling the
        reviewer's structured verdict — the heartbeat.decide idiom: the
        judgement and the reviewer's own reason for it become an
        append-only row in the run's story, distinct from the gate outcome
        that acts on it. The verdict also lands on the run row: the fact
        that this review *concluded*, which is what keeps its PR off the
        heartbeat's backlog whatever the gate then decides."""
        await asyncio.to_thread(self._runs.record_verdict, run_id, verdict)
        await self._append_event(
            run_id=run_id,
            tool=REVIEW_VERDICT,
            payload={"verdict": verdict, "rationale": rationale},
            decision="allow" if verdict == "pass" else "deny",
            rule=f"verdict_{verdict}",
            rationale=rationale[:RATIONALE_LIMIT],
            source="process-lifecycle",
            workflow_id=workflow_id,
        )
        self._log.bind(run_id=run_id).info("verdict recorded", extra={"verdict": verdict})

    @activity.defn(name="reviewer_independent")
    async def reviewer_independent(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """The registry-checked fact behind `reviewer_independent`: the
        review run's identity lineage shares nothing with the lineage of
        the implement run that delivered the PR — neither an agent_id
        (impossible while agent_id stays the participants' primary key,
        but the check is positional, not schema-trusting) nor a crew
        template: a blueprint may legally bind review to the delivery
        crew, and that is exactly the self-review this check refuses to
        call independent. Fail closed on absence — a PR no implement run
        attributes, or a review with no bound reviewer, is not eligible
        for an automated merge (the human signal still is)."""

        def read() -> dict[str, Any]:
            implement = self._runs.implement_run_for_pr(self._company(), pr_number)
            if implement is None:
                return {
                    "independent": False,
                    "reason": f"no implement run attributes PR #{pr_number}",
                }
            reviewers = self._participants.for_run(run_id)
            implementers = self._participants.for_run(implement.run_id)
            if not reviewers:
                return {
                    "independent": False,
                    "reason": "no reviewer identity bound to this run",
                }
            shared_ids = sorted(
                {p["agent_id"] for p in reviewers} & {p["agent_id"] for p in implementers}
            )
            if shared_ids:
                return {
                    "independent": False,
                    "reason": f"shared identities with {implement.run_id}: "
                    + ", ".join(shared_ids),
                }
            shared_crews = sorted(
                {p["crew_template"] for p in reviewers}
                & {p["crew_template"] for p in implementers}
            )
            if shared_crews:
                return {
                    "independent": False,
                    "reason": f"crew {', '.join(shared_crews)} both delivered "
                    f"and reviews this change ({implement.run_id})",
                }
            return {"independent": True, "reason": f"lineage {implement.run_id}"}

        result = await asyncio.to_thread(read)
        self._log.bind(run_id=run_id).info("independence checked", extra=result)
        return result

    @activity.defn(name="rework_depth")
    async def rework_depth(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """How many reworks deep this PR's chain already is, plus the
        chain's root PR — read from the canonical title of the ticket the
        failing PR's implement run consumed. The rework bound must follow
        the *chain*, not the single PR: every rework delivers a fresh PR,
        so a per-PR count would reset to zero each round and the loop
        would never terminate. A PR outside any chain (fresh work, an
        unattributable PR, a free-form ticket) is depth 0 with itself as
        root.

        Read from the *consumed* ticket, not from any freely-filed one, so
        a standalone ticket forging a low depth changes nothing (proven in
        test_review_activities). The title it trusts is the platform's own
        `file_rework` output, and no company role can rewrite it: the tool
        that would edit an issue's title is not in any compiled allow-list,
        so a compromised conductor cannot walk a rework ticket's depth
        downward. A depth the platform stores on its own row would be more
        robust still, but the mutation path the parse-back would have to
        fear does not exist."""

        issue_number = await asyncio.to_thread(
            consumed_ticket, self._runs, self._company(), pr_number
        )
        if issue_number is None:
            return {"depth": 0, "root_pr": pr_number}
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issue = await gateway.read_issue(
                run_id, (await self._repo())["url"], issue_number
            )
        match = REWORK_TITLE_RE.match(str(issue.get("title") or ""))
        if match is None:
            return {"depth": 0, "root_pr": pr_number}
        depth = {"depth": int(match.group(1)), "root_pr": int(match.group(2))}
        self._log.bind(run_id=run_id).info("rework depth read", extra=depth)
        return depth

    @activity.defn(name="file_rework")
    async def file_rework(
        self,
        run_id: str,
        pr_number: int,
        root_pr: int,
        attempt: int,
        reason: str,
        findings: str,
        pr_title: str,
        pr_url: str,
        paths: list[str],
    ) -> dict[str, Any]:
        """File the failed review's findings back into the backlog as a
        ticket, under the platform identity, and record it on the review
        run's row (the same `ticket_urls` breadcrumb a scope run leaves). The
        conductor consumes it on a later heartbeat like any other issue.
        Retry honesty: issue creation is not idempotent, so a retried
        create can file a duplicate ticket — a noisy backlog, never a
        lost finding."""
        title = rework_title(attempt, root_pr, pr_title)
        body = rework_body(pr_number, pr_url, reason, findings, paths, attempt, run_id)
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issue = await gateway.create_issue(
                run_id,
                (await self._repo())["url"],
                title,
                body,
                f"{reason}: {findings}"[:RATIONALE_LIMIT],
            )
        url = str(issue.get("url") or "")
        if url:
            await asyncio.to_thread(self._runs.record_artifacts, run_id, None, [url])
        self._log.bind(run_id=run_id).info(
            "rework ticket filed",
            extra={"url": url, "attempt": attempt, "root_pr": root_pr},
        )
        return {"url": url, "number": issue.get("number")}

    @activity.defn(name="warn_contract_edit")
    async def warn_contract_edit(
        self,
        run_id: str,
        pr_number: int,
        deletions: dict[str, int],
        workflow_id: str,
    ) -> None:
        """The oracle-edit surfacing, both channels at once: a platform
        comment on the PR (what the human reads before resolving the merge
        signal) and a `review.contract_edit` audit event carrying the same
        counts — a gutted spec must be impossible to approve silently."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            await gateway.comment_pr(
                run_id,
                (await self._repo())["url"],
                pr_number,
                contract_edit_comment(deletions),
            )
        await self._append_event(
            run_id=run_id,
            tool=REVIEW_CONTRACT_EDIT,
            payload=deletions,
            decision="allow",
            rule="contract_edit:"
            + ",".join(f"{k}={v}" for k, v in sorted(deletions.items())),
            source="process-lifecycle",
            workflow_id=workflow_id,
        )
        self._log.bind(run_id=run_id).info(
            "contract edit surfaced", extra={"pr_number": pr_number, **deletions}
        )

    @activity.defn(name="read_human_verdict")
    async def read_human_verdict(self, run_id: str) -> dict[str, Any] | None:
        """The console's latest recorded verdict on this run's merge gate —
        `{principal, approved, ts}` — or None when no human answered through
        the console. Read by the platform off its own audit store, so the
        fact `merge.resolve` decides on is one the workflow never took from
        the signal: a signal is a nudge to look; the record is the verdict."""
        verdict = await asyncio.to_thread(
            self._audit.latest_human_verdict, run_id, MERGE_GATE
        )
        if verdict is not None:
            verdict = {**verdict, "ts": str(verdict["ts"])}
        return verdict

    @activity.defn(name="resolve_merge")
    async def resolve_merge(
        self,
        run_id: str,
        resolver: str,
        facts: dict[str, Any],
        workflow_id: str,
    ) -> dict[str, Any]:
        """Evaluate the compiled `merge.resolve` decision — the
        heartbeat_authorize idiom: the caller-verified facts ride the input,
        the compiled literals bind as policy, and the answer is audited
        either way. On the `automated` resolver the facts are the
        automation facts (verdict, checks, independence, the contract fact,
        the changed paths); on the `human` resolver the one fact is the
        console-recorded verdict. Unlike heartbeat.start, a deny is a
        routed outcome, not an error: good work outside the automated
        paths falls back to the human signal, and an unrecorded human
        signal ends the run unmerged."""
        company = self._company()
        decision_input = {
            "company": company,
            "agent_id": ORCHESTRATOR_ID,
            "lifecycle": LIFECYCLE_PLATFORM,
            "parent_agent_id": "",
            "role": PLATFORM_ROLE,
            "run_id": run_id,
            "workflow": "review",
            "tool": MERGE_RESOLVE,
            "args": {"resolver": resolver, **facts},
            "run_binding_verified": False,
        }
        started = time.monotonic()
        result = await evaluate_decision(self._settings.opa_url, company, decision_input)
        allowed = bool(result.get("allow"))
        await self._append_event(
            run_id=run_id,
            tool=MERGE_RESOLVE,
            payload=decision_input["args"],
            decision="allow" if allowed else "deny",
            rule=str(result.get("rule") or ""),
            source="process-lifecycle",
            workflow_id=workflow_id,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        self._log.bind(run_id=run_id).info(
            "merge resolve evaluated",
            extra={"allow": allowed, "rule": result.get("rule")},
        )
        return {"allow": allowed, "rule": str(result.get("rule") or "")}

    @activity.defn(name="merge_pr")
    async def merge_pr(
        self, run_id: str, pr_number: int, head_sha: str
    ) -> dict[str, Any]:
        """The post-gate merge: a gateway tool call under the
        orchestrator's platform identity — audited at the gateway — that
        the workflow issues strictly after the merge gate approved. It
        names the head the run reviewed: a head that moved since merges
        nothing and answers the host's refusal (`refused`), which the
        workflow turns into a refused delivery — no retry brings the
        reviewed head back."""
        started = time.monotonic()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.merge_pr(
                run_id, (await self._repo())["url"], pr_number, head_sha
            )
        self._log.bind(run_id=run_id).info(
            "pr merged",
            extra={
                "pr_number": pr_number,
                "merged": result.get("merged"),
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        if not result.get("merged") and not result.get("refused"):
            raise RuntimeError(f"merge of pull request #{pr_number} did not land")
        return result

    @activity.defn(name="close_superseded_pr")
    async def close_superseded_pr(
        self, run_id: str, pr_number: int, ticket_number: int
    ) -> dict[str, Any]:
        """Close the PR whose failed review the rework ticket now carries,
        under the platform identity: the ticket supersedes it, and an open
        PR nobody will review again is debris on the host. Its diff and
        findings stay readable through `git.read` — the ticket names it.
        Superseded is not refused: the rework ticket carries the work on,
        so the ticket this PR delivered stays concluded."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.close_pr(
                run_id,
                (await self._repo())["url"],
                pr_number,
                f"superseded by rework ticket #{ticket_number}",
                delete_branch=False,
            )
        await asyncio.to_thread(
            self._runs.mark_pr_settled, self._company(), pr_number, PR_SUPERSEDED
        )
        self._log.bind(run_id=run_id).info(
            "superseded pr closed",
            extra={"pr_number": pr_number, "ticket": ticket_number},
        )
        return result

    @activity.defn(name="refuse_delivery")
    async def refuse_delivery(self, run_id: str, pr_number: int, reason: str) -> None:
        """The merge gate ended without a merge or a rework: the PR closes
        with the reason on it, and its ticket goes back on the backlog as
        one spent attempt, told why (`tickets.refuse_delivery`)."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            await refuse_delivery(
                gateway,
                self._runs,
                self._company(),
                run_id,
                (await self._repo())["url"],
                pr_number,
                reason,
                await asyncio.to_thread(self._max_attempts),
            )

    @activity.defn(name="close_tickets")
    async def close_tickets(
        self, run_id: str, pr_number: int, sha: str, pr_title: str
    ) -> list[int]:
        """Close the tickets the just-merged PR fulfils, under the platform
        identity, with the merge as the rationale. Returns the numbers
        closed."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            return await close_fulfilled_tickets(
                gateway,
                self._runs,
                self._company(),
                run_id,
                (await self._repo())["url"],
                pr_number,
                f"merged pull request #{pr_number} ({sha}): {pr_title}",
            )

    def all(self) -> list[Any]:
        return [
            self.read_pr,
            self.pr_tips,
            self.contract_tree,
            self.record_verdict,
            self.reviewer_independent,
            self.warn_contract_edit,
            self.read_human_verdict,
            self.resolve_merge,
            self.rework_depth,
            self.file_rework,
            self.close_superseded_pr,
            self.refuse_delivery,
            self.merge_pr,
            self.close_tickets,
        ]
