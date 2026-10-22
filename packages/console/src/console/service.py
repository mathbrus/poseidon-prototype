"""The console's service: every human act as verify → decide → do → audit,
and the reads beside them.

An act is a decision first. The verified person's identity and the act's
facts are one decision input to the company's compiled bundle — the same
shape the gateway sends for a tool call — and a deny is a `PolicyDenied`
the app answers 403 with the rule's name, audited like an allow. The one
act policy cannot decide is the first apply: no bundle exists for a
company before its Company CR installs one, so when OPA serves no policy
for the company any verified human may apply, and the audit row says so
(`blueprint_apply_bootstrap`).

A gate verdict is recorded before it is delivered: the audit row under
the person's name is the artifact the review workflow reads back and
`merge.resolve` decides on, and the Temporal signal that follows is only
a nudge to look. Row first, signal second, so the read can never race
the signal.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

import yaml

from blueprint import validate_blueprint
from blueprint.validate import ValidationError
from common.audit import AuditEvent, args_digest
from common.console import (
    ApplyResult,
    CompanyDeleted,
    CompanyView,
    EventsPage,
    HeartbeatView,
    Now,
    PauseState,
    PreviewResult,
    RunCancelled,
    RunDetail,
    RunList,
    RunSpendView,
    RunStarted,
    SpendView,
    StoryView,
    VerdictRecorded,
    Woken,
)
from common.identity import Identity
from common.ids import new_run_id
from common.opa import NO_POLICY_RULE, evaluate_decision
from common.policy import (
    BLUEPRINT_APPLY,
    COMPANY_DELETE,
    GATE_RESOLVE,
    HEARTBEAT_PAUSE,
    HEARTBEAT_RESUME,
    HEARTBEAT_WAKE,
    RUN_CANCEL,
    RUN_START,
    verdict_rule,
)
from common.telemetry import get_logger
from common.workflows import (
    GATES_OF,
    PARKED_STATE,
    heartbeat_run_id,
    workflow_id,
)
from compiler import (
    DeploymentPlan,
    InstanceSnapshot,
    RegistrySnapshot,
    compile_blueprint,
    diff_plan,
)
from compiler.errors import CompileError
from console import views
from console.config import ConsoleSettings
from console.temporal import AlreadyStarted, WorkflowNotFound, Workflows
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    RunRecord,
    Runs,
)

BOOTSTRAP_RULE = "blueprint_apply_bootstrap"


class PolicyDenied(Exception):
    def __init__(self, rule: str) -> None:
        super().__init__(f"denied by policy: {rule}")
        self.rule = rule


class Invalid(Exception):
    """The request names something that cannot be acted on: an unknown
    run, a gate or workflow that does not exist, a blueprint that does
    not validate or names a company other than the applied one."""


class NoCompany(Exception):
    """An act or a read that needs a company, on a cluster where none is
    applied yet. The blueprint apply is the one act that never raises it."""


class ConsoleService:
    def __init__(
        self,
        settings: ConsoleSettings,
        audit: AuditStore,
        runs: Runs,
        ledger: BudgetLedger,
        instances: AgentInstances,
        companies: Companies,
        workflows: Workflows,
        submit_crs: Callable[[DeploymentPlan], tuple[int, int, int]],
        delete_company: Callable[[str, bool], None],
        read_company: Callable[[], dict[str, Any] | None],
        clock: Callable[[], str],
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._runs = runs
        self._ledger = ledger
        self._instances = instances
        self._companies = companies
        self._workflows = workflows
        self._submit = submit_crs
        self._delete = delete_company
        self._read_company = read_company
        self._clock = clock
        self._log = get_logger("console")

    # -- the applied company --------------------------------------------------

    async def applied(self) -> str | None:
        """The company this cluster hosts — the one the reconciler recorded
        from the applied Company CR — or None before the first apply."""
        return await asyncio.to_thread(self._companies.applied)

    async def _company(self) -> str:
        company = await self.applied()
        if company is None:
            raise NoCompany("no company is applied")
        return company

    # -- verify → decide → do → audit ---------------------------------------

    async def _decide(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        company: str,
        run_id: str = "",
    ) -> dict[str, Any]:
        decision_input = {
            "company": company,
            "agent_id": identity.agent_id,
            "lifecycle": identity.lifecycle,
            "parent_agent_id": "",
            "role": identity.role,
            "run_id": run_id,
            "workflow": "",
            "tool": tool,
            "args": args,
            "run_binding_verified": False,
        }
        return await evaluate_decision(self._settings.opa_url, company, decision_input)

    async def _record(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        decision: dict[str, Any],
        *,
        company: str,
        run_id: str = "",
        workflow: str = "",
        started: float,
        rationale: str | None = None,
    ) -> None:
        event = AuditEvent(
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=tool,
            args_digest=args_digest(args),
            decision="allow" if decision.get("allow") else "deny",
            rule=str(decision.get("rule") or ""),
            rationale=rationale,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=None,
            source="console",
            company=company,
            lifecycle=identity.lifecycle,
            workflow=workflow,
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.info("audit", extra=event.model_dump())

    async def _act(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        *,
        company: str,
        run_id: str = "",
        workflow: str = "",
        bootstrap_rule: str | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Decide the act and audit the answer; raise on deny. Returns the
        decision so a caller can name the allowing rule. The company is
        the caller's to name: every act but the first apply reads it from
        the registry, and the apply reads it from the plan. The rationale
        is the person's own reason, recorded on the row whatever policy
        answered."""
        started = time.monotonic()
        decision = await self._decide(identity, tool, args, company, run_id)
        if (
            bootstrap_rule is not None
            and not decision.get("allow")
            and decision.get("rule") == NO_POLICY_RULE
        ):
            decision = {"allow": True, "rule": bootstrap_rule}
        await self._record(
            identity,
            tool,
            args,
            decision,
            company=company,
            run_id=run_id,
            workflow=workflow,
            started=started,
            rationale=rationale,
        )
        if not decision.get("allow"):
            raise PolicyDenied(str(decision.get("rule") or "default_deny"))
        return decision

    # -- the blueprint --------------------------------------------------------

    def _plan(self, blueprint_text: str) -> tuple[DeploymentPlan, RegistrySnapshot]:
        try:
            document = yaml.safe_load(blueprint_text)
        except yaml.YAMLError as exc:
            raise Invalid(f"blueprint is not YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise Invalid("blueprint is not a mapping")
        try:
            validate_blueprint(document)
        except ValidationError as exc:
            raise Invalid(str(exc)) from exc
        name = document["company"]["name"]
        strangers = sorted(
            set(document["company"]["humans"]) - set(self._settings.humans)
        )
        if strangers and self._settings.humans:
            # The console verifies humans; it does not make them. Where the
            # chart declares who a person can be, a blueprint may only name
            # those people — anyone else could never log in to act.
            raise Invalid(
                f"company.humans names {', '.join(repr(s) for s in strangers)}, "
                f"whom this console cannot verify "
                f"(declared: {', '.join(sorted(self._settings.humans))})"
            )
        applied = self._companies.applied()
        if applied is not None and name != applied:
            # A platform hosts one company. The name is not a field to
            # re-apply into: the namespace, the bundle slot and the
            # standing agents are the applied company's until it is deleted.
            raise Invalid(
                f"this cluster hosts {applied!r}; delete it before applying {name!r}"
            )
        snapshot = RegistrySnapshot(
            instances=tuple(
                InstanceSnapshot(
                    agent_id=row["agent_id"],
                    company=row["company"],
                    standing_name=row["standing_name"],
                    role=row["role"],
                    status=row["status"],
                )
                for row in self._instances.for_company(name)
            )
        )
        try:
            plan = compile_blueprint(document, snapshot=snapshot)
        except CompileError as exc:
            raise Invalid(str(exc)) from exc
        return plan, snapshot

    async def preview(self, blueprint_text: str) -> PreviewResult:
        """Compile against the live registry and say what applying would
        change. A read: nothing is decided, nothing is written."""
        plan, snapshot = await asyncio.to_thread(self._plan, blueprint_text)
        return PreviewResult(
            company=plan.company.name, diff=diff_plan(plan, snapshot).as_text()
        )

    async def apply(self, identity: Identity, blueprint_text: str) -> ApplyResult:
        plan, snapshot = await asyncio.to_thread(self._plan, blueprint_text)
        decision = await self._act(
            identity,
            BLUEPRINT_APPLY,
            {"company": plan.company.name},
            company=plan.company.name,
            bootstrap_rule=BOOTSTRAP_RULE,
        )
        created, updated, generation = await asyncio.to_thread(self._submit, plan)
        return ApplyResult(
            company=plan.company.name,
            generation=generation,
            created=created,
            updated=updated,
            diff=diff_plan(plan, snapshot).as_text(),
            rule=decision["rule"],
        )

    async def company(self) -> CompanyView:
        """The applied company as the Company CR tells it: its phase
        (`activating` until OPA serves the stored bundles, `converged`
        after), the generation that phase answers for, the bundle
        revision; and the pause as the registry tells it (`{by, at}`, or
        None). What `apply` waits on, and what `company show` prints."""
        cr = await asyncio.to_thread(self._read_company)
        if cr is None:
            raise NoCompany("no company is applied")
        paused = await asyncio.to_thread(self._companies.paused, cr["metadata"]["name"])
        return views.company_view(cr, paused)

    async def delete(
        self, identity: Identity, rationale: str, wait: bool = True
    ) -> CompanyDeleted:
        """Stop the company: the one stop there is (a pause is not one —
        the company stays, held). Decided and audited under the person's
        name first; then every running run is cancelled through the same
        path `cancel_run` takes, so none dies with its namespace; then the
        Company CR goes and the reconciler's teardown — the tombstone served,
        the rows swept, the namespace deleted — is waited for unless the
        caller said not to (it proceeds either way). The heartbeat stops
        itself at its next config read. What the company wrote into the
        world (its repository, its ledger rows) stays: applying the same
        name again brings it back."""
        company = await self._company()
        started = time.monotonic()
        decision = await self._act(
            identity,
            COMPANY_DELETE,
            {},
            company=company,
            rationale=rationale,
        )
        cancelled = []
        for record in await asyncio.to_thread(self._runs.running, company):
            try:
                await self.cancel_run(
                    identity, record.run_id, f"company deleted: {rationale}"
                )
            except Invalid:
                continue  # a run whose workflow is already gone needs no cancel
            cancelled.append(record.run_id)
        await asyncio.to_thread(self._delete, company, wait)
        return CompanyDeleted(
            company=company,
            runs_cancelled=cancelled,
            elapsed_s=int(time.monotonic() - started),
            rule=decision["rule"],
        )

    # -- runs -----------------------------------------------------------------

    async def start_run(
        self, identity: Identity, workflow: str, number: int
    ) -> RunStarted:
        """Start one implement or review run by hand — a person reproducing
        a run on a text the company produced. The heartbeat starts the
        company's own runs; this bypasses its caps, which is why it is a
        decided act. Which workflows a human may start is the request's
        to say (`StartableWorkflow`)."""
        company = await self._company()
        run_id = new_run_id()
        wf_id = workflow_id(workflow, run_id)
        decision = await self._act(
            identity,
            RUN_START,
            {"workflow": workflow, "number": number},
            company=company,
            run_id=run_id,
            workflow=wf_id,
        )
        key = "issue_number" if workflow == "implement" else "pr_number"
        try:
            await self._workflows.start(workflow, run_id, {"run_id": run_id, key: number})
        except AlreadyStarted as exc:
            raise Invalid(f"a workflow already runs as {wf_id}") from exc
        return RunStarted(
            run_id=run_id, workflow=workflow, workflow_id=wf_id, rule=decision["rule"]
        )

    def _run_of(self, run_id: str) -> tuple[RunRecord, str]:
        """The run's row and the workflow id that addresses it."""
        record = self._runs.get(run_id)
        if record is None:
            raise Invalid(f"no run {run_id!r}")
        return record, views.run_workflow_id(record)

    async def resolve_gate(
        self,
        identity: Identity,
        run_id: str,
        gate: str,
        approved: bool,
        rationale: str,
    ) -> VerdictRecorded:
        """Record the verdict under the person's name, with their reason as
        the row's rationale, then deliver it. The allowing rule names the
        gate and the verdict (`gate_resolve_merge_approve`), which is
        exactly what the workflow reads back; the row is the record, so the
        signal carries the verdict alone. A verdict answers a question that
        was asked: the run's workflow must have the gate and be parked on
        it."""
        record, wf_id = await asyncio.to_thread(self._run_of, run_id)
        if gate not in GATES_OF.get(record.workflow, ()):
            raise Invalid(f"a {record.workflow} run has no {gate} gate")
        try:
            execution = await self._workflows.inspect(wf_id)
        except WorkflowNotFound as exc:
            raise Invalid(f"run {run_id!r} has no workflow to signal") from exc
        if execution["state"] != PARKED_STATE[gate]:
            raise Invalid(
                f"run {run_id!r} is not parked on its {gate} gate"
                f" (state {execution['state']!r})"
            )
        decision = await self._act(
            identity,
            GATE_RESOLVE,
            {"gate": gate, "run_id": run_id, "approved": approved},
            company=record.company,
            run_id=run_id,
            workflow=wf_id,
            rationale=rationale,
        )
        if decision["rule"] != verdict_rule(gate, approved):
            # The compiled allow is the record the workflow reads back; a
            # bundle that allowed under any other name would record nothing
            # readable, so refuse rather than deliver an unreadable verdict.
            raise RuntimeError(f"gate.resolve allowed under {decision['rule']!r}")
        try:
            await self._workflows.signal(wf_id, gate, [approved])
        except WorkflowNotFound as exc:
            raise Invalid(f"run {run_id!r} has no workflow to signal") from exc
        return VerdictRecorded(
            run_id=run_id,
            gate=gate,
            approved=approved,
            human=identity.agent_id,
            rule=decision["rule"],
        )

    async def cancel_run(
        self, identity: Identity, run_id: str, rationale: str
    ) -> RunCancelled:
        record, wf_id = await asyncio.to_thread(self._run_of, run_id)
        decision = await self._act(
            identity,
            RUN_CANCEL,
            {"run_id": run_id},
            company=record.company,
            run_id=run_id,
            workflow=wf_id,
            rationale=rationale,
        )
        try:
            await self._workflows.cancel(wf_id)
        except WorkflowNotFound as exc:
            raise Invalid(f"run {run_id!r} has no workflow to cancel") from exc
        return RunCancelled(run_id=run_id, workflow_id=wf_id, rule=decision["rule"])

    async def _execution(self, wf_id: str):
        """The workflow's execution, or the answer for one Temporal does
        not know."""
        try:
            return views.execution_view(await self._workflows.inspect(wf_id))
        except WorkflowNotFound:
            return views.missing_execution(wf_id)

    async def run(self, run_id: str) -> RunDetail:
        record, wf_id = await asyncio.to_thread(self._run_of, run_id)
        return views.run_detail(record, await self._execution(wf_id))

    async def runs_since(self, since: str | None) -> RunList:
        company = await self._company()
        records = await asyncio.to_thread(self._runs.since, company, since)
        return RunList(runs=[views.run_view(r) for r in records])

    async def story(self, run_id: str, decisions: bool = False) -> StoryView:
        """The run's audit rows in order; with `decisions`, only those that
        carry a rationale — the choices and their reasons."""
        rows = await asyncio.to_thread(self._audit.story, run_id, None, decisions)
        return views.story_view(run_id, rows)

    async def events(self, after: int, run_id: str | None, limit: int) -> EventsPage:
        """The company's audit rows above a cursor, in id order, with the
        cursor to continue from and the spend to date — what a follower
        polls. Spend is metered into the budget ledger, not the audit
        ledger, hence its own figure: the company's total, and the run's
        when the read is narrowed to one."""
        company = await self._company()
        rows = await asyncio.to_thread(self._audit.after, company, after, run_id, limit)
        total = await asyncio.to_thread(self._ledger.spent_since, company, None)
        run_usd = None
        if run_id:
            run_usd = await asyncio.to_thread(self._ledger.spent_for_run, run_id)
        return views.events_page(rows, after, total["usd"], run_usd)

    async def spend(self, run_id: str) -> RunSpendView:
        usd = await asyncio.to_thread(self._ledger.spent_for_run, run_id)
        entries = await asyncio.to_thread(self._ledger.entries_for_run, run_id)
        return views.run_spend_view(run_id, usd, entries)

    async def spend_since(self, since: str | None) -> SpendView:
        company = await self._company()
        total = await asyncio.to_thread(self._ledger.spent_since, company, since)
        return views.spend_view(total)

    # -- the heartbeat --------------------------------------------------------

    async def heartbeat(self, since: str | None) -> HeartbeatView:
        """The company's standing loop: its execution, and its ticks since
        a fence — the rows the loop journals under the shared heartbeat
        run id, each naming the rule its tick ended on."""
        company = await self._company()
        execution = await self._execution(workflow_id("heartbeat", company))
        story = await asyncio.to_thread(
            self._audit.story, heartbeat_run_id(company), since
        )
        return views.heartbeat_view(company, execution, story)

    async def wake(self, identity: Identity) -> Woken:
        """A nudge that something changed the world the loop watches —
        never an instruction. The tick that results observes, decides and
        is authorized like any other."""
        company = await self._company()
        wf_id = workflow_id("heartbeat", company)
        decision = await self._act(
            identity,
            HEARTBEAT_WAKE,
            {},
            company=company,
            run_id=heartbeat_run_id(company),
            workflow=wf_id,
        )
        try:
            await self._workflows.signal(wf_id, "wake", [])
        except WorkflowNotFound as exc:
            raise Invalid(f"no heartbeat is running for {company!r}") from exc
        return Woken(company=company, workflow_id=wf_id, rule=decision["rule"])

    async def pause(self, identity: Identity, rationale: str) -> PauseState:
        """Pause the loop under the person's name. The order is the
        verdict's: decided and audited first, then the pause written where
        every tick reads it, then a wake so the loop records the pause now
        and `heartbeat show` says `paused`. Runs in flight are not the
        loop's and are not touched. A heartbeat not running yet is no
        failure: the worker starts it, and it reads the pause then."""
        company = await self._company()
        if await asyncio.to_thread(self._companies.paused, company):
            raise Invalid(f"{company!r} is already paused")
        rule = await self._hold(identity, HEARTBEAT_PAUSE, rationale, company)
        paused = await asyncio.to_thread(
            self._companies.pause, company, identity.agent_id
        )
        await self._nudge(company)
        return PauseState(company=company, paused=views.pause_view(paused), rule=rule)

    async def resume(self, identity: Identity, rationale: str) -> PauseState:
        """Lift the pause: decided and audited, the registry cleared, the
        loop woken so its next tick looks at the world at once."""
        company = await self._company()
        if not await asyncio.to_thread(self._companies.paused, company):
            raise Invalid(f"{company!r} is not paused")
        rule = await self._hold(identity, HEARTBEAT_RESUME, rationale, company)
        await asyncio.to_thread(self._companies.resume, company)
        await self._nudge(company)
        return PauseState(company=company, paused=None, rule=rule)

    async def _hold(
        self, identity: Identity, act: str, rationale: str, company: str
    ) -> str:
        """A pause or a resume as an act on the heartbeat's own story;
        the rule it was allowed under."""
        decision = await self._act(
            identity,
            act,
            {},
            company=company,
            run_id=heartbeat_run_id(company),
            workflow=workflow_id("heartbeat", company),
            rationale=rationale,
        )
        return decision["rule"]

    async def _nudge(self, company: str) -> None:
        """Wake the loop if it runs; a loop not running reads the registry
        when the worker starts it."""
        try:
            await self._workflows.signal(workflow_id("heartbeat", company), "wake", [])
        except WorkflowNotFound:
            pass

    async def now(self) -> Now:
        """The registry's clock — the fence a reader takes before acting so
        its reads are its own."""
        return Now(now=await asyncio.to_thread(self._clock))
