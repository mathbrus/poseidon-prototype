"""The review activities: thin clients, as always —
sequencing, the merge gate and its timers live in the workflow.

Retry safety, per activity:
    create_review_run   — upserts the run row; safe.
    review_config       — read-only; safe.
    read_pr             — read-only against GitHub via the gateway; safe.
    record_gate_outcome — appends an audit event; a retried append
                          duplicates a row, never loses one (the audit
                          store is append-only by design).
    record_verdict      — appends the reviewer's journaled verdict; same
                          append-only posture.
    reviewer_independent— read-only against the registry; safe.
    resolve_merge       — re-evaluates the compiled merge.resolve decision
                          and re-audits; the honest record of what was
                          asked (the conduct_authorize posture).
    merge_pr            — GitHub's merge endpoint is idempotent for an
                          already-merged PR (it answers 405, surfaced as a
                          backend error the workflow reports honestly).

The merge itself: `git.merge_pr` is a *gateway tool
call* under the orchestrator's platform identity — one of the two mediation
paths, audited at the gateway like any agent call — issued by the workflow
only after the human gate approved. The gate outcome events here are the
`process-gate` audit source: approvals are workflow state, so the
audit story is where a human can later see that the gate, not a bug,
released the merge.
"""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

from temporalio import activity

from common.audit import AuditEvent
from common.telemetry import get_logger
from orchestrator.clients import GatewayClient
from orchestrator.config import WorkerSettings
from orchestrator.opa import evaluate_decision
from registry import AuditStore, RunParticipants, Runs
from registry.bundles import AgentInstances, Companies


class ReviewActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances | None = None,
        audit: AuditStore | None = None,
        participants: RunParticipants | None = None,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._instances = instances
        self._audit = audit
        self._participants = participants
        self._log = get_logger("orchestrator.review")

    @activity.defn(name="create_review_run")
    async def create_review_run(self, run_id: str, pr_number: int) -> None:
        from orchestrator.workflows.review import review_workflow_id

        await asyncio.to_thread(
            self._runs.start,
            run_id,
            f"review pull request #{pr_number}",
            "review",
            review_workflow_id(run_id),
            self._settings.company,
        )
        self._log.bind(run_id=run_id).info(
            "review run created", extra={"pr_number": pr_number}
        )

    @activity.defn(name="review_config")
    async def review_config(self) -> dict[str, Any]:
        """The review workflow's shape, from the applied blueprint:
        which crew reviews, which standing conductor's authority the crew
        request is recorded under, and which signal the merge gate parks
        on — gate config comes from the compiled plan, never hardcoded.
        Fail-closed, like implement_config."""

        def read() -> dict[str, Any]:
            spec = self._companies.get(self._settings.company) or {}
            config = next(
                (w for w in spec.get("workflows") or [] if w.get("name") == "review"),
                None,
            )
            if config is None or not config.get("enabled", False):
                raise RuntimeError(
                    f"company {self._settings.company!r} has no enabled"
                    " review workflow in its applied blueprint"
                )
            crew_template = config.get("crew")
            if not crew_template:
                raise RuntimeError(
                    "review workflow binds no crew — the reviewer is an"
                    " ephemeral crew member; the compiler should"
                    " have refused this blueprint"
                )
            conductor_hire = config["conductor"]
            instance = None
            if self._instances is not None:
                instance = next(
                    (
                        i
                        for i in self._instances.for_company(self._settings.company)
                        if i.get("hire") == conductor_hire
                    ),
                    None,
                )
            if instance is None:
                raise RuntimeError(
                    f"no standing instance for conductor hire"
                    f" {conductor_hire!r} — is the company converged?"
                )
            return {
                "company": self._settings.company,
                "crew_template": crew_template,
                "conductor": instance["agent_id"],
                "conductor_role": instance["role_template"],
                # The gate object as the blueprint wrote it (`{resolved_by,
                # delegate?}`), or None when no merge gate is declared.
                "merge_gate": (config.get("gates") or {}).get("merge"),
            }

        return await asyncio.to_thread(read)

    @activity.defn(name="read_pr")
    async def read_pr(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """PR metadata under the platform identity: the head ref is what
        the review sandbox is provisioned from, the title and URL
        seed the reviewer's task text and the run record."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            pr = await gateway.read_pr(run_id, self._settings.target_repo, pr_number)
        if not pr.get("head"):
            raise RuntimeError(
                f"pull request #{pr_number} has no head ref — nothing to"
                " provision a review sandbox from"
            )
        # The reviewer reads the diff itself (git.read is in its scope);
        # the workflow only needs the shape, not the patches — plus the
        # changed paths, which the merge-time class check resolves against.
        shaped = {k: v for k, v in pr.items() if k != "files"}
        shaped["paths"] = sorted(
            {f["path"] for f in pr.get("files") or [] if f.get("path")}
        )
        return shaped

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
        latency_ms: int = 0,
    ) -> None:
        """The one AuditEvent constructor for this module's producers —
        append-only, so a retried append duplicates a row, never loses
        one."""
        if self._audit is None:
            raise RuntimeError("review activities constructed without an audit store")
        event = AuditEvent(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            agent_id="orchestrator",
            role="platform",
            tool=tool,
            args_digest=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            decision=decision,  # type: ignore[arg-type]
            rule=rule,
            latency_ms=latency_ms,
            backend_status=None,
            source=source,  # type: ignore[arg-type]
            company=self._settings.company,
            lifecycle="platform",
            parent_agent_id="",
            workflow=workflow_id,
        )
        await asyncio.to_thread(self._audit.append, event)

    @activity.defn(name="record_gate_outcome")
    async def record_gate_outcome(
        self, run_id: str, gate: str, kind: str, workflow_id: str
    ) -> None:
        """One `source=process-gate` audit event: parked gates,
        escalations and verdicts become first-class rows in the run's
        story, carrying the workflow id that maps back to run_id. The
        policy kinds keep human and policy resolutions distinguishable
        forever: `policy_resolved` released a merge, `policy_declined`
        fell back to the human signal."""
        await self._append_event(
            run_id=run_id,
            tool=f"gate.{gate}",
            payload={"gate": gate, "outcome": kind},
            decision="allow" if kind in ("approved", "policy_resolved") else "deny",
            rule=f"gate_{kind}",
            source="process-gate",
            workflow_id=workflow_id,
        )
        self._log.bind(run_id=run_id).info(
            "gate outcome recorded", extra={"gate": gate, "outcome": kind}
        )

    @activity.defn(name="record_verdict")
    async def record_verdict(
        self, run_id: str, verdict: str, summary: str, workflow_id: str
    ) -> None:
        """One `source=process-lifecycle` audit event journaling the
        reviewer's structured verdict — the conduct.decide idiom: the
        judgement and its one-line why become an append-only row in the
        run's story, distinct from the gate outcome that acts on it."""
        await self._append_event(
            run_id=run_id,
            tool="review.verdict",
            payload={"verdict": verdict, "summary": summary},
            decision="allow" if verdict == "pass" else "deny",
            rule=summary[:300],
            source="process-lifecycle",
            workflow_id=workflow_id,
        )
        self._log.bind(run_id=run_id).info(
            "verdict recorded", extra={"verdict": verdict}
        )

    @activity.defn(name="reviewer_independent")
    async def reviewer_independent(
        self, run_id: str, pr_number: int
    ) -> dict[str, Any]:
        """The registry-checked fact behind `reviewer_independent`: the
        review run's identity lineage shares nothing with the lineage of
        the implement run that delivered the PR — neither an agent_id
        (impossible while agent_id stays the participants' primary key,
        but the check is positional, not schema-trusting) nor a crew
        template: a blueprint may legally bind review to the delivery
        crew, and that is exactly the self-review this check refuses to
        call independent. Fail closed on absence — a PR no implement run
        attributes, or a review with no bound reviewer, is not eligible
        for a policy-resolved merge (the human signal still is)."""

        def read() -> dict[str, Any]:
            implement = self._runs.implement_run_for_pr(
                self._settings.company, pr_number
            )
            if implement is None:
                return {
                    "independent": False,
                    "reason": f"no implement run attributes PR #{pr_number}",
                }
            if self._participants is None:
                raise RuntimeError(
                    "review activities constructed without participants"
                )
            reviewers = self._participants.for_run(run_id)
            implementers = self._participants.for_run(implement.run_id)
            if not reviewers:
                return {
                    "independent": False,
                    "reason": "no reviewer identity bound to this run",
                }
            shared_ids = sorted(
                {p["agent_id"] for p in reviewers}
                & {p["agent_id"] for p in implementers}
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

    @activity.defn(name="resolve_merge")
    async def resolve_merge(
        self,
        run_id: str,
        verdict: str,
        checks_green: bool,
        reviewer_independent: bool,
        paths: list[str],
        workflow_id: str,
    ) -> dict[str, Any]:
        """Evaluate the compiled `merge.resolve` decision — the
        conduct_authorize idiom: the caller-verified facts ride the input,
        the delegated class and carve-outs bind as policy, and the answer
        is audited either way. Unlike conduct.start, a deny is a routed
        outcome, not an error: good work outside the delegation falls
        back to the human signal."""
        company = self._settings.company
        decision_input = {
            "company": company,
            "agent_id": "orchestrator",
            "lifecycle": "platform",
            "parent_agent_id": "",
            "role": "platform",
            "run_id": run_id,
            "workflow": "review",
            "tool": "merge.resolve",
            "args": {
                "verdict": verdict,
                "checks_green": checks_green,
                "reviewer_independent": reviewer_independent,
                "paths": paths,
            },
            "run_binding_verified": False,
        }
        started = time.monotonic()
        result = await evaluate_decision(self._settings.opa_url, company, decision_input)
        allowed = bool(result.get("allow"))
        await self._append_event(
            run_id=run_id,
            tool="merge.resolve",
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
    async def merge_pr(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """The post-gate merge: a gateway tool call under the
        orchestrator's platform identity — audited at the gateway — that
        the workflow issues strictly after the merge gate approved."""
        started = time.monotonic()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.merge_pr(run_id, self._settings.target_repo, pr_number)
        self._log.bind(run_id=run_id).info(
            "pr merged",
            extra={
                "pr_number": pr_number,
                "merged": result.get("merged"),
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        if not result.get("merged"):
            raise RuntimeError(f"merge of pull request #{pr_number} did not land")
        return result

    def all(self) -> list[Any]:
        return [
            self.create_review_run,
            self.review_config,
            self.read_pr,
            self.record_gate_outcome,
            self.record_verdict,
            self.reviewer_independent,
            self.resolve_merge,
            self.merge_pr,
        ]
