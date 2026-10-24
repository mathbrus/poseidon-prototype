"""The review workflow: PR in, human-gated merge out.

    create_run -> check_budget -> workflow_config -> read_pr (the
    PR's live state under the platform identity; a merged or closed PR
    is refused here as `pr_not_open`, before any sandbox is spent) ->
    fetch_workspace (the PR's head commit, pinned for the whole run) ->
    provision_sandbox -> platform_verify (under a delegated gate only,
    before any agent holds the sandbox; red files rework and ends the run
    with no reviewer dispatched) ->
    fulfill_crew (feature-review under the PM's conductor authority —
    process-initiated is a first-class recorded request,
    not a bypass) -> dispatch the ephemeral reviewer (the review procedure:
    reads the diff via git.read, runs the tests in its own sandbox,
    posts findings via git.comment, ends with a structured pass/fail
    verdict) -> record_verdict (the journaled judgement) -> MERGE GATE ->
    approve: the orchestrator merges that same head commit via
    git.merge_pr under its platform identity, then closes the tickets the
    PR fulfilled (issues.close); reject/timeout: honest failure ->
    teardown.

The merge gate owns the merge guarantee, and the blueprint names its
resolver — gate config comes from the compiled plan, never hardcoded.
`resolved_by: human` parks on the `merge` signal. `resolved_by: policy`
adds the platform's own layers before anything merges: the platform
verify of the PR head, run before the reviewer in the sandbox as
provisioned (the same activity that gates implement delivery), the
reviewer's journaled verdict, the contract verify when a
contract tree is declared (the base ref's contract tests, composed over
the PR head and run scoped — a baseline nothing in the PR can rewrite),
the registry-checked reviewer-independence fact, and one evaluation of
the compiled merge.resolve decision — allow releases the merge, deny
falls back to the human signal, so the human override survives
delegation. A diff touching the contract tree itself skips the layers
entirely: it can never policy-merge (the compiled carve-out), so the
gate declines up front and the PR parks on the human with the
deletion facts already posted — a decline, not a failure. Nobody
home at a park? The timer escalates at half the deadline and expires the
gate at the end; the park is API-visible (`state` query), every
transition is a `process-gate` audit event written through an activity,
and the merge itself is a gateway-audited tool call — so one run_id
query tells the whole story: findings, verdict, checks, resolution,
merge.

Review is orchestrator-sequenced over HTTP. A human reject stays the
honest minimum: findings are already on the PR, the run finalizes failed
with the gate's verdict. Under a policy-resolved gate, a failed quality
layer instead feeds the bounded rework loop: the findings become a
backlog ticket the conductor consumes, the platform closes the PR the
ticket supersedes (git.close_pr), the chain depth rides the canonical
ticket title (read back by the platform, never trusted from an agent),
and at `max_reworks` the PR escalates to the human park instead of
looping forever. When a rework PR finally merges, the whole chain's
tickets close with it.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from common.tasks import review_run_task
from common.workflows import (
    BUDGET_GATE,
    GATE_APPROVED,
    GATE_ESCALATED,
    GATE_PARKED,
    GATE_POLICY_DECLINED,
    GATE_POLICY_RESOLVED,
    GATE_REWORK_EXHAUSTED,
    GATE_REWORK_FILED,
    GATE_VERDICT_UNVERIFIED,
    MERGE_GATE,
    PARKED_STATE,
    RUN_FINALIZING,
    RUN_RUNNING,
)
from orchestrator.gates import Gate
from orchestrator.workflows.base import (
    BOOTSTRAP_S,
    STEP_RETRY,
    RunWorkflow,
    gate_deadline,
)


@dataclass
class ReviewInput:
    run_id: str
    pr_number: int


def review_task(pr_number: int, title: str, url: str) -> str:
    """The reviewer's dispatch text — composed by workflow code from the
    PR the platform read, so it is deterministic on replay."""
    return (
        f"Review pull request #{pr_number} ({url}): {title}\n\n"
        "Read the diff with the read_pr tool, run the test suite in your"
        " sandbox, and post your findings on the pull request as a single"
        " comment_pr call. Findings, not fixes: you review this change,"
        " you do not rewrite it."
    )


@workflow.defn(name="review")
class ReviewWorkflow(RunWorkflow):
    WORKFLOW = "review"

    def __init__(self) -> None:
        self._state = RUN_RUNNING
        self._budget_gate = Gate(BUDGET_GATE)
        self._merge_gate = Gate(MERGE_GATE)

    @workflow.signal
    def budget(self, approved: bool) -> None:
        """The console's budget verdict: resolve a budget park, exactly as
        in implement."""
        self._budget_gate.resolve(approved)

    @workflow.signal
    def merge(self, approved: bool) -> None:
        """The console's merge verdict, the signal the merge gate parks on.
        A nudge to look, not the verdict itself: the release reads the
        verdict the console recorded under the human's name."""
        self._merge_gate.resolve(approved)

    @workflow.query
    def state(self) -> str:
        """The API-visible run phase: a parked merge gate must be a
        visible pending state, never a silent hang."""
        return self._state

    def _touches_contract(self, pr: dict[str, Any], config: dict[str, Any]) -> bool:
        """Whether the platform-read diff edits the declared contract tree.
        Such a PR can never policy-merge: the compiled carve-out denies its
        resolve whatever the facts say (pinned in the bundle tests), and the
        workflow knows the same thing positionally, from the
        blueprint-declared tree and the diff, before it spends anything."""
        contract_paths = config.get("contract_paths") or []
        return any(
            path.startswith(prefix)
            for path in pr.get("paths") or []
            for prefix in contract_paths
        )

    async def _verify(self, input: "ReviewInput") -> dict[str, Any]:
        """The platform's own run of the PR head's suite, in the sandbox
        provisioned from that head and untouched since — the same activity
        that gates implement delivery: one verification concept, two
        callers. It runs before any agent holds the sandbox, so nothing
        the reviewer does there can move it; the reviewer's own run_tests
        is advisory."""
        return await workflow.execute_activity(
            "platform_verify",
            args=[input.run_id],
            start_to_close_timeout=timedelta(seconds=self._profile.verify_s),
            retry_policy=STEP_RETRY,
        )

    async def _policy_merge(
        self,
        input: "ReviewInput",
        pr: dict[str, Any],
        result: dict[str, Any],
        config: dict[str, Any],
        verify: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The policy-resolved path, after the reviewer and a green
        platform verify (`verify`, run before the reviewer was dispatched).
        A diff touching the declared contract tree declines: it is the
        human's to merge, so no layer is spent on it and no rework ticket
        is filed on a PR only the human can merge. It parks on the `merge`
        signal with the deletion facts already posted.

        Everything else: quality layers first — a failed verdict or a red
        contract verify feeds the bounded rework loop (`_rework`): findings
        become a backlog ticket the conductor consumes, until the chain is
        out of attempts and the PR escalates to the human. Then one
        evaluation of the compiled merge.resolve decision, carrying the
        caller-verified facts (verdict, checks, independence, contract) and
        the changed paths: allow releases the merge under the platform
        identity; deny is a *decline*, not a failure — good work outside
        the delegation falls back to the human signal, so the human
        override survives delegation by construction.

        The verified tree, the reviewed tree and the merged commit are one
        head, pinned at the run's start: every sandbox is provisioned from
        the head commit read then, and the merge names it, so a push after
        that point merges nothing."""
        gate = config["gates"].get(MERGE_GATE) or {}
        if self._touches_contract(pr, config):
            # Decline, not failure: park. At bootstrap this also keeps the
            # first contract PR off the composed baseline, which has no
            # tree to run yet.
            await self.record_gate(input.run_id, MERGE_GATE, GATE_POLICY_DECLINED)
            return {}
        verdict = result["verdict"]
        if verdict != "pass":
            return await self._rework(
                input,
                pr,
                gate,
                "review verdict failed",
                result.get("verdict_rationale") or "",
            )
        # The contract layer, when the company declares a contract tree
        # and the base ref holds it (`contract_tree`): the platform
        # composes head code with the contract tree as it stands on the
        # base ref and runs exactly those tests. The head suite (hygiene,
        # run before the reviewer) can be weakened by the PR; nothing in
        # this baseline came from the PR, so it cannot. A declared tree not
        # yet on the base ref replays nothing and leaves the fact vacuously
        # true. Re-provisioning replaces the reviewer's finished sandbox in
        # place — the same deterministic name, so teardown's compensation
        # is unchanged.
        contract_paths = config.get("contract_paths") or []
        contract_green = True
        contract_tree: list[str] = []
        if contract_paths:
            contract_tree = await workflow.execute_activity(
                "contract_tree",
                args=[input.run_id, contract_paths],
                start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
                retry_policy=STEP_RETRY,
            )
        if contract_tree:
            composed = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id, pr["head_sha"], contract_tree],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
            await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, composed],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
            contract = await workflow.execute_activity(
                "platform_verify",
                args=[input.run_id, contract_tree],
                start_to_close_timeout=timedelta(seconds=self._profile.verify_s),
                retry_policy=STEP_RETRY,
            )
            contract_green = bool(contract.get("passed"))
            if not contract_green:
                return await self._rework(
                    input,
                    pr,
                    gate,
                    "contract verify red",
                    (contract.get("output") or "")[-1000:],
                )
        independence = await workflow.execute_activity(
            "reviewer_independent",
            args=[input.run_id, input.pr_number],
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )
        resolve = await self._resolve(
            input,
            "policy",
            {
                "verdict": verdict,
                "checks_green": bool(verify and verify.get("passed")),
                "reviewer_independent": bool(independence.get("independent")),
                # The composed-baseline fact: the contract tree as it
                # stands on the base ref, run against this PR's head.
                # Vacuously true only when no contract tree is declared —
                # the compiled rule then never reads it.
                "contract_green": contract_green,
                "paths": pr.get("paths") or [],
            },
        )
        if not resolve.get("allow"):
            await self.record_gate(input.run_id, MERGE_GATE, GATE_POLICY_DECLINED)
            return {}
        return await self._release(input, pr)

    async def _rework(
        self,
        input: "ReviewInput",
        pr: dict[str, Any],
        gate: dict[str, Any],
        reason: str,
        findings: str,
    ) -> dict[str, Any]:
        """A failed quality layer feeds the backlog, bounded. The chain
        depth is read from the failing PR's own lineage (never trusted
        from an agent); under the blueprint's `max_reworks` the findings
        become a ticket the conductor consumes, and the run finalizes
        failed with the ticket on its row. At the bound, the PR escalates
        to the human instead of looping forever: a `rework_exhausted`
        process-gate event, then the ordinary human park."""
        depth = await workflow.execute_activity(
            "rework_depth",
            args=[input.run_id, input.pr_number],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )
        limit = int((gate.get("automate") or {}).get("max_reworks") or 0)
        if depth["depth"] >= limit:
            await self.record_gate(input.run_id, MERGE_GATE, GATE_REWORK_EXHAUSTED)
            return {}
        ticket = await workflow.execute_activity(
            "file_rework",
            args=[
                input.run_id,
                input.pr_number,
                depth["root_pr"],
                depth["depth"] + 1,
                reason,
                findings,
                pr.get("title") or "",
                pr.get("url") or "",
                pr.get("paths") or [],
            ],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )
        # The ticket supersedes the PR: the platform closes it, naming the
        # ticket, so the host carries no PR nobody will review again.
        await workflow.execute_activity(
            "close_superseded_pr",
            args=[input.run_id, input.pr_number, ticket.get("number")],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )
        await self.record_gate(input.run_id, MERGE_GATE, GATE_REWORK_FILED)
        return {
            "error": f"policy merge gate: {reason} — rework ticket filed:"
            f" {ticket.get('url')}"
        }

    async def _merge_pr(self, input: "ReviewInput", pr: dict[str, Any]) -> dict[str, Any]:
        """The merge itself — one platform tool call, issued only after a
        gate (human or policy) resolved to merge. Both resolvers land here,
        so there is exactly one place the branch reaches `main`, and it
        names the head this run read: what was reviewed is what merges."""
        return await workflow.execute_activity(
            "merge_pr",
            args=[input.run_id, input.pr_number, pr["head_sha"]],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )

    async def _resolve(
        self, input: "ReviewInput", resolver: str, facts: dict[str, Any]
    ) -> dict[str, Any]:
        """One evaluation of the compiled `merge.resolve` on the named
        path, audited by the activity either way."""
        return await workflow.execute_activity(
            "resolve_merge",
            args=[input.run_id, resolver, facts, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )

    async def _human_merge(
        self, input: "ReviewInput", pr: dict[str, Any], gate: dict[str, Any]
    ) -> dict[str, Any]:
        """The human gate: the declared `resolved_by: human` resolver, and
        the fallback whenever a policy gate declines or its rework chain is
        spent. Park on the signal, escalate at half the deadline, expire at
        the end — every transition a process-gate audit event — and return
        the same `{released}|{error}` shape the policy path does, so the run
        has one merge-application site.

        An approving signal is not the release. The release is policy's:
        the verdict the console recorded under the human's name is read
        back from the audit store and `merge.resolve` decides on it — so a
        signal delivered by any other road (network reach to the workflow
        engine, a human the company did not name) finds no recorded
        verdict and ends the run unmerged, by a rule that says so."""
        # The row first, then the state: a run that reads parked always has
        # the row its deadlines are measured from.
        await self.record_gate(input.run_id, MERGE_GATE, GATE_PARKED)
        self._state = PARKED_STATE[MERGE_GATE]
        outcome = await self._merge_gate.wait(
            **gate_deadline(gate["deadline_minutes"]),
            on_escalate=lambda: self.record_gate(
                input.run_id, MERGE_GATE, GATE_ESCALATED
            ),
        )
        self._state = RUN_RUNNING
        await self.record_gate(input.run_id, MERGE_GATE, outcome.kind)
        if outcome.kind != GATE_APPROVED:
            return {"error": f"merge gate {outcome.kind}"}
        verdict = await workflow.execute_activity(
            "read_human_verdict",
            args=[input.run_id],
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )
        resolve = await self._resolve(
            input, "human", {"human_verdict": verdict} if verdict else {}
        )
        if not resolve.get("allow"):
            await self.record_gate(input.run_id, MERGE_GATE, GATE_VERDICT_UNVERIFIED)
            return {
                "error": f"merge gate: no verified human verdict ({resolve.get('rule')})"
            }
        return {"released": await self._merge_pr(input, pr)}

    async def _release(self, input: "ReviewInput", pr: dict[str, Any]) -> dict[str, Any]:
        """The policy release: gate outcome first, then the merge — strictly
        after the compiled allow."""
        await self.record_gate(input.run_id, MERGE_GATE, GATE_POLICY_RESOLVED)
        return {"released": await self._merge_pr(input, pr)}

    @workflow.run
    async def run(self, input: ReviewInput) -> dict[str, Any]:
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        # Whether the reviewer reached a verdict — the review's conclusion,
        # whatever the gate then does with it. A run that ends without one
        # aborted, and the attempt accounting below decides whether the PR
        # stays on the backlog.
        concluded = False
        try:
            config = await self.start_run(input.run_id, review_run_task(input.pr_number))
            pr = await workflow.execute_activity(
                "read_pr",
                args=[input.run_id, input.pr_number, config.get("contract_paths") or []],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
            # An oracle edit is surfaced the moment it is seen — a platform
            # comment on the PR plus an audit event — so whoever resolves
            # the eventual human park reads the deletion facts first.
            if pr.get("contract_deletions"):
                await workflow.execute_activity(
                    "warn_contract_edit",
                    args=[
                        input.run_id,
                        input.pr_number,
                        pr["contract_deletions"],
                        workflow.info().workflow_id,
                    ],
                    start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                    retry_policy=STEP_RETRY,
                )
            # A full sandbox from the PR's head commit — the reviewer runs
            # things, it does not just read diffs. Reuses the implement
            # provisioning verbatim.
            workspace_tar = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id, pr["head_sha"]],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
            handle = await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, workspace_tar],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
            gate = config["gates"].get(MERGE_GATE) or {}
            automated = "automate" in gate
            # Under an automated gate the platform verifies the head first,
            # in the sandbox as provisioned: the reviewer holds a shell
            # there, so a verify run after it could read whatever the
            # reviewer left. A contract-tree PR is the human's whatever the
            # suite says, so nothing is spent on it.
            verify: dict[str, Any] | None = None
            if automated and not self._touches_contract(pr, config):
                verify = await self._verify(input)
                if not verify.get("passed"):
                    rework = await self._rework(
                        input,
                        pr,
                        gate,
                        "platform verify red",
                        (verify.get("output") or "")[-1000:],
                    )
                    if rework.get("error"):
                        # The red suite is the review's conclusion: the
                        # rework ticket carries it and no reviewer is
                        # dispatched on code the platform already refused.
                        concluded = True
                        result = {"pr_url": pr["url"]}
                        error = rework["error"]
                        raise _ReviewOver()
                    # The rework chain is spent: the reviewer still runs, so
                    # the human park carries its findings, and the policy
                    # layers are skipped — the PR goes to the human.
                    automated = False
            # The review crew is fulfilled under the conductor's authority
            # — the compiled crew.request decision, evaluated as the
            # conductor — and reads the company's documents as they stand
            # on base_ref, untrusted like the diff itself.
            reviewer = await self.fulfill(input.run_id, config)
            result = await self.dispatch(
                input.run_id,
                reviewer["url"],
                "review",
                review_task(input.pr_number, pr["title"], pr["url"]),
                await self.dispatch_documents(input.run_id),
            )
            if (result or {}).get("error"):
                raise _ReviewOver()
            if not (result or {}).get("verdict"):
                error = "reviewer finished without a verdict"
                raise _ReviewOver()
            # Journal the verdict before the gate reads anything: an
            # append-only record of what the review concluded, whatever
            # the gate then decides — the heartbeat.decide idiom.
            await workflow.execute_activity(
                "record_verdict",
                args=[
                    input.run_id,
                    result["verdict"],
                    result.get("verdict_rationale") or "",
                    workflow.info().workflow_id,
                ],
                start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
                retry_policy=STEP_RETRY,
            )
            concluded = True
            # The run record links the PR under review whatever the gate
            # decides — the review happened either way.
            result = dict(result or {})
            result["pr_url"] = pr["url"]

            if not gate:
                # The blueprint declared no merge gate: the findings are
                # the deliverable and the workflow ends without merging —
                # an unattended auto-merge is not a default anyone gets
                # implicitly. (Distinct from a policy gate: no rework, no
                # park; an advisory review.)
                raise _ReviewOver()

            # One dispatch, two resolvers with one contract. A policy gate
            # runs its layers first: a failed layer files a bounded rework
            # and ends the run (its `error`); an allow releases the merge; a
            # decline or spent rework chain leaves `{}`, which falls through
            # to the human gate — the same gate a review without `automate`
            # uses, so the human signal resolves every gate and the
            # override survives automation.
            outcome: dict[str, Any] = {}
            if automated:
                outcome = await self._policy_merge(input, pr, result, config, verify)
                if outcome.get("error"):
                    error = outcome["error"]
                    raise _ReviewOver()
            if not outcome.get("released"):
                outcome = await self._human_merge(input, pr, gate)
                if outcome.get("error"):
                    error = outcome["error"]
                    raise _ReviewOver()
            result["merged"] = True
            result["merge_sha"] = outcome["released"].get("sha")
            # The merge fulfilled the PR's ticket (a rework chain's, all of
            # them): the platform closes them, with the merge as its reason.
            # A close that finally fails ends the run in error with the
            # merge already on the record.
            result["closed_tickets"] = await workflow.execute_activity(
                "close_tickets",
                args=[
                    input.run_id,
                    input.pr_number,
                    result["merge_sha"] or "",
                    pr.get("title") or "",
                ],
                start_to_close_timeout=timedelta(seconds=self._profile.step_s),
                retry_policy=STEP_RETRY,
            )
        except _ReviewOver:
            pass
        except asyncio.CancelledError:
            cancelled = True
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._state = RUN_FINALIZING
        record = await self.teardown(input.run_id, handle, result, error)
        if not concluded:
            await self.record_attempts(input.run_id)
        # However this run ended, it changed the world the standing
        # heartbeat watches — nudge it; the reconcile timer covers a
        # nudge that goes missing.
        await self.wake_heartbeat()
        if cancelled:
            raise asyncio.CancelledError()
        return record


class _ReviewOver(Exception):
    """Internal control flow: a terminal verdict (reviewer error, missing
    verdict, rework filed, gate reject/timeout, or a gate-free finish) skips
    straight to teardown with whatever result/error is in hand."""
