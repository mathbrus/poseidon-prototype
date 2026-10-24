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
from common.audit import AuditEvent, args_digest, gate_event
from common.console import (
    EXECUTION_RUNNING,
    ActList,
    ActView,
    AgentList,
    ApplyResult,
    BlueprintView,
    BoundsView,
    CompanyDeleted,
    CompanyView,
    CrewView,
    DocumentView,
    EventsPage,
    ExecutionView,
    HeartbeatView,
    Now,
    ParkedView,
    PauseState,
    PreviewResult,
    RunCancelled,
    RunDetail,
    RunList,
    RunSpendView,
    RunStarted,
    SpendView,
    StoryView,
    TicketList,
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
from common.topology import BLUEPRINT_LIMIT_BYTES, TEARDOWN_TIMEOUT_S
from common.workflows import (
    GATE_PARKED,
    GATES_OF,
    HEARTBEAT_PAUSED,
    HEARTBEAT_WORKFLOW,
    HUMAN_STARTABLE,
    RUN_STATUS_RUNNING,
    gate_rule,
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
from console import acts, crs, views
from console.config import ConsoleSettings
from console.documents import Documents
from console.temporal import AlreadyStarted, WorkflowNotFound, Workflows
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    FiledTickets,
    RunParticipants,
    RunRecord,
    Runs,
)

BOOTSTRAP_RULE = "blueprint_apply_bootstrap"
# How often a delete asks again whether what it waits on has happened: the
# heartbeat holding, a cancelled run's workflow closing.
DELETE_POLL_S = 2

# What a human start works on, per workflow: the reader of the attempts
# that hold an item, what the item is called, and the payload key naming it.
_WORK_ITEMS = {
    "implement": ("issue_attempts", "ticket", "issue_number"),
    "review": ("review_attempts", "pull request", "pr_number"),
}


class PolicyDenied(Exception):
    def __init__(self, rule: str) -> None:
        super().__init__(acts.policy_detail(rule))
        self.rule = rule


class Invalid(Exception):
    """The request names something that cannot be acted on: an unknown
    run, a gate or workflow that does not exist, a blueprint that does
    not validate or names a company other than the applied one."""


class NoCompany(Exception):
    """An act or a read that needs a company, on a cluster where none is
    applied yet. The blueprint apply is the one act that never raises it."""


class Unfinished(Exception):
    """An act that began and could not finish in time: what it did stands,
    and asking again finishes it."""


class ConsoleService:
    def __init__(
        self,
        settings: ConsoleSettings,
        audit: AuditStore,
        runs: Runs,
        ledger: BudgetLedger,
        instances: AgentInstances,
        companies: Companies,
        participants: RunParticipants,
        tickets: FiledTickets,
        workflows: Workflows,
        documents: Documents,
        submit_crs: Callable[[DeploymentPlan, dict[str, str]], tuple[int, int, int]],
        delete_company: Callable[[str, float], None],
        read_company: Callable[[], dict[str, Any] | None],
        clock: Callable[[], str],
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._runs = runs
        self._ledger = ledger
        self._instances = instances
        self._companies = companies
        self._participants = participants
        self._tickets = tickets
        self._workflows = workflows
        self._documents = documents
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

    async def _spec(self, company: str) -> dict[str, Any] | None:
        """The applied spec the platform enforces — the reconciler's copy,
        there once the company has converged."""
        return await asyncio.to_thread(self._companies.get, company)

    async def _converged_spec(self) -> tuple[str, dict[str, Any]]:
        company = await self._company()
        spec = await self._spec(company)
        if spec is None:
            raise NoCompany(f"{company!r} has not converged yet")
        return company, spec

    # -- verify → decide → do → audit ---------------------------------------

    async def _decision(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        company: str,
        run_id: str = "",
    ) -> dict[str, Any]:
        """What policy answers this person for this act — the one question
        an act and its preview both ask. The one act policy cannot decide
        is the first apply: no bundle is served for a company before its
        Company CR installs one, so the bootstrap rule allows it."""
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
        decision = await evaluate_decision(
            self._settings.opa_url, company, decision_input
        )
        if tool == BLUEPRINT_APPLY and decision.get("rule") == NO_POLICY_RULE:
            return {"allow": True, "rule": BOOTSTRAP_RULE}
        return decision

    async def _act(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        *,
        company: str,
        run_id: str = "",
        workflow: str = "",
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Decide the act and audit the answer; raise on deny. Returns the
        decision so a caller can name the allowing rule. The company is
        the caller's to name: every act but the first apply reads it from
        the registry, and the apply reads it from the plan. The rationale
        is the person's own reason, recorded on the row whatever policy
        answered."""
        started = time.monotonic()
        decision = await self._decision(identity, tool, args, company, run_id)
        refused = acts.refused_rule(decision)
        event = AuditEvent(
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=tool,
            args_digest=args_digest(args),
            decision="deny" if refused else "allow",
            rule=refused or str(decision["rule"]),
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
        if refused:
            raise PolicyDenied(refused)
        return decision

    # -- the blueprint --------------------------------------------------------

    def _plan(self, blueprint_text: str) -> tuple[DeploymentPlan, RegistrySnapshot]:
        size = len(blueprint_text.encode())
        if size > BLUEPRINT_LIMIT_BYTES:
            # The CR keeps the text; the limit keeps the CR storable.
            raise Invalid(
                f"the blueprint is {size} bytes; the most a blueprint may be is"
                f" {BLUEPRINT_LIMIT_BYTES}"
            )
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
        if applied is not None:
            # A company works on one repository for its life: its runs,
            # tickets and attempts are history on that repository, and a
            # new one would inherit what it never did.
            stored = ((self._companies.get(applied) or {}).get("repo") or {}).get("url")
            url = document["stack"]["repo"]["url"]
            if stored and url != stored:
                raise Invalid(
                    f"{applied!r} works on {stored}; its repository cannot change "
                    f"to {url} — delete the company, then apply the blueprint"
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
            acts.apply_args(plan.company.name),
            company=plan.company.name,
        )
        # The text itself rides the Company CR, the record of intent: the
        # compiled spec is not the blueprint and cannot be applied again.
        source = crs.blueprint_source(
            blueprint_text, identity.agent_id, await asyncio.to_thread(self._clock)
        )
        created, updated, generation = await asyncio.to_thread(self._submit, plan, source)
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
        company = cr["metadata"]["name"]
        paused, spec, spent = await asyncio.gather(
            asyncio.to_thread(self._companies.paused, company),
            self._spec(company),
            asyncio.to_thread(self._ledger.spent_today, company),
        )
        limits = views.limits_view(spec, spent) if spec is not None else None
        return views.company_view(cr, paused, limits)

    async def blueprint(self) -> BlueprintView:
        """The blueprint's text as it was applied, from the Company CR."""
        cr = await asyncio.to_thread(self._read_company)
        view = views.blueprint_view(cr) if cr is not None else None
        if view is None:
            raise NoCompany("no blueprint is applied")
        return view

    async def document(self, kind: str) -> DocumentView:
        """One of the two documents at the base ref, read live through the
        MCP gateway under the console's own identity."""
        _, spec = await self._converged_spec()
        repo = spec["repo"]
        read = await self._documents.read(
            repo["url"], repo["base_ref"], spec["documents"][kind]
        )
        read_at = await asyncio.to_thread(self._clock)
        return views.document_view(kind, spec, read, read_at)

    async def bounds(self) -> BoundsView:
        """The bounds every dispatch carries, from the applied spec."""
        company, spec = await self._converged_spec()
        return views.bounds_view(company, spec)

    async def agents(self) -> AgentList:
        company, spec = await self._converged_spec()
        rows = await asyncio.to_thread(self._instances.for_company, company)
        return views.agent_list(rows, spec["heartbeat"]["conductor"])

    async def tickets(self) -> TicketList:
        _, spec = await self._converged_spec()
        repo = spec["repo"]["url"]
        return views.ticket_list(repo, await asyncio.to_thread(self._tickets.filed, repo))

    async def delete(self, identity: Identity, rationale: str) -> CompanyDeleted:
        """Delete the company: the one stop there is (a pause is not one —
        the company stays, held), and it erases everything the platform
        recorded about the company. In order:

        1. decided and audited under the person's name;
        2. the heartbeat held — the pause `heartbeat.pause` sets, with no
           act of its own — and waited for until it says so, so no tick
           starts a run meanwhile;
        3. every running run cancelled through the path `cancel_run`
           takes, and waited for until its workflow is closed, its own
           teardown done;
        4. the Company CR deleted and the reconciler's teardown waited for:
           the tombstone served, the namespace gone, every registry row of
           the company swept, this act's own rows with them;
        5. the company's workflow histories deleted from Temporal, the
           heartbeat's whole continue-as-new chain included.

        The whole delete waits at most `TEARDOWN_TIMEOUT_S`. A step that
        cannot finish fails the act with what was done standing, the
        company held and its runs ended, and a second delete finishes it:
        every step is safe to repeat. The answer is the receipt."""
        company = await self._company()
        started = time.monotonic()
        deadline = started + TEARDOWN_TIMEOUT_S
        decision = await self._act(
            identity,
            COMPANY_DELETE,
            acts.NO_ARGS,
            company=company,
            rationale=rationale,
        )
        await self._hold_for_delete(identity, company, deadline)
        cancelled = await self._end_runs(identity, company, rationale, deadline)
        histories = await self._workflow_ids(company)
        try:
            await asyncio.to_thread(
                self._delete, company, max(0.0, deadline - time.monotonic())
            )
        except TimeoutError as exc:
            raise Unfinished(str(exc)) from exc
        await self._erase_histories(company, histories)
        return CompanyDeleted(
            company=company,
            runs_cancelled=cancelled,
            elapsed_s=int(time.monotonic() - started),
            rule=decision["rule"],
        )

    async def _hold_for_delete(
        self, identity: Identity, company: str, deadline: float
    ) -> None:
        """Hold the heartbeat under the person's name and wake it, then
        wait until it says it is held: a tick that began before the hold
        may still start a run, which the cancels after this find. A loop
        not running has no tick to wait for."""
        if await asyncio.to_thread(self._companies.paused, company) is None:
            await asyncio.to_thread(self._companies.pause, company, identity.agent_id)
        await self._nudge(company)
        await self._until(
            workflow_id(HEARTBEAT_WORKFLOW, company),
            lambda execution: (
                execution.status != EXECUTION_RUNNING
                or execution.state == HEARTBEAT_PAUSED
            ),
            deadline,
            "the heartbeat has not held",
        )

    async def _end_runs(
        self, identity: Identity, company: str, rationale: str, deadline: float
    ) -> list[str]:
        """Cancel every running run, then wait until each workflow has
        closed, its crew and sandbox torn down. The run ids cancelled."""
        cancelled = []
        for record in await asyncio.to_thread(self._runs.running, company):
            try:
                result = await self.cancel_run(
                    identity, record.run_id, f"company deleted: {rationale}"
                )
            except Invalid:
                continue  # a run whose workflow is already gone needs no cancel
            cancelled.append((record.run_id, result.workflow_id))
        for run_id, wf_id in cancelled:
            await self._until(
                wf_id,
                lambda execution: execution.status != EXECUTION_RUNNING,
                deadline,
                f"run {run_id!r} has not finished",
            )
        return [run_id for run_id, _ in cancelled]

    async def _until(
        self,
        wf_id: str,
        done: Callable[[ExecutionView], bool],
        deadline: float,
        unfinished: str,
    ) -> None:
        while not done(await self._execution(wf_id)):
            if time.monotonic() >= deadline:
                raise Unfinished(
                    f"{unfinished} after {TEARDOWN_TIMEOUT_S}s; what the delete did"
                    " stands: delete the company again to finish it"
                )
            await asyncio.sleep(DELETE_POLL_S)

    async def _workflow_ids(self, company: str) -> list[str]:
        """Every workflow the company ran: its heartbeat and each run's,
        read before the sweep erases the rows that name them."""
        records = await asyncio.to_thread(self._runs.since, company, None)
        ids = [workflow_id(HEARTBEAT_WORKFLOW, company)]
        ids += [views.run_workflow_id(record) for record in records]
        return list(dict.fromkeys(ids))

    async def _erase_histories(self, company: str, ids: list[str]) -> None:
        """End the heartbeat if it still runs (the worker no longer
        restarts it once the company's row is gone), then delete every
        execution under the company's workflow ids."""
        try:
            await self._workflows.terminate(
                workflow_id(HEARTBEAT_WORKFLOW, company), "company deleted"
            )
        except WorkflowNotFound:
            pass  # already ended
        for wf_id in ids:
            await self._workflows.delete(wf_id)

    # -- runs -----------------------------------------------------------------

    async def start_run(
        self, identity: Identity, workflow: str, number: int
    ) -> RunStarted:
        """Start one implement or review run by hand — a person reproducing
        a run on a text the company produced. The heartbeat starts the
        company's own runs; this bypasses its caps, which is why it is a
        decided act. Which workflows a human may start is the request's
        to say (`StartableWorkflow`).

        One live run per work item: a ticket or a pull request a live run
        already holds is refused here, before policy is asked, rather than
        started into the registry's one-live-run index — which stays the
        backstop for a start that races this read."""
        company = await self._company()
        reader, item, key = _WORK_ITEMS[workflow]
        attempts = await asyncio.to_thread(getattr(self._runs, reader), company)
        if number in attempts and attempts[number].live:
            raise Invalid(
                f"a live {workflow} run already holds {item} #{number};"
                " one live run per work item"
            )
        run_id = new_run_id()
        wf_id = workflow_id(workflow, run_id)
        decision = await self._act(
            identity,
            RUN_START,
            acts.start_args(workflow, number),
            company=company,
            run_id=run_id,
            workflow=wf_id,
        )
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
        refusal = acts.verdict_refusal(record, gate, await self._execution(wf_id))
        if refusal is not None:
            raise Invalid(refusal)
        decision = await self._act(
            identity,
            GATE_RESOLVE,
            acts.verdict_args(gate, run_id, approved),
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
        refusal = acts.cancel_refusal(record, await self._execution(wf_id))
        if refusal is not None:
            raise Invalid(refusal)
        decision = await self._act(
            identity,
            RUN_CANCEL,
            acts.cancel_args(run_id),
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

    async def _execution(self, wf_id: str) -> ExecutionView:
        """The workflow's execution, or the answer for one Temporal does
        not know."""
        try:
            return views.execution_view(await self._workflows.inspect(wf_id))
        except WorkflowNotFound:
            return views.missing_execution(wf_id)

    async def _deadlines(self, company: str) -> dict[str, int]:
        """How long each gate waits, by the applied spec; none before the
        company has converged."""
        spec = await self._spec(company)
        return views.gate_deadlines(spec) if spec else {}

    async def _inspected(
        self, record: RunRecord, deadlines: dict[str, int]
    ) -> tuple[ExecutionView, ParkedView | None]:
        """The run's execution and, if it is parked, where it waits: since
        its latest `parked` gate row, for the gate's deadline."""
        execution = await self._execution(views.run_workflow_id(record))
        gate = views.parked_gate(execution)
        if gate is None:
            return execution, None
        since = await asyncio.to_thread(
            self._audit.last_at, record.run_id, gate_event(gate), gate_rule(GATE_PARKED)
        )
        return execution, views.parked_view(gate, since, deadlines.get(gate))

    async def run(self, run_id: str) -> RunDetail:
        record, _ = await asyncio.to_thread(self._run_of, run_id)
        execution, parked = await self._inspected(
            record, await self._deadlines(record.company or "")
        )
        return views.run_detail(record, execution, parked)

    async def runs_since(self, since: str | None, parked_only: bool = False) -> RunList:
        """The company's runs since a fence. The ones still running are
        inspected, all at once, so each says whether it is parked."""
        company = await self._company()
        records, deadlines = await asyncio.gather(
            asyncio.to_thread(self._runs.since, company, since),
            self._deadlines(company),
        )
        running = [r for r in records if r.status == RUN_STATUS_RUNNING]
        inspected = await asyncio.gather(
            *(self._inspected(r, deadlines) for r in running)
        )
        parked = {r.run_id: park for r, (_, park) in zip(running, inspected, strict=True)}
        listed = [views.run_view(r, parked.get(r.run_id)) for r in records]
        if parked_only:
            listed = [view for view in listed if view.parked is not None]
        return RunList(runs=listed)

    async def crew(self, run_id: str) -> CrewView:
        """Every ephemeral agent ever bound to the run, revoked included."""
        await asyncio.to_thread(self._run_of, run_id)
        rows = await asyncio.to_thread(self._participants.for_run, run_id)
        return views.crew_view(run_id, rows)

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
        refusal = acts.wake_refusal(company, await self._execution(wf_id))
        if refusal is not None:
            raise Invalid(refusal)
        decision = await self._act(
            identity,
            HEARTBEAT_WAKE,
            acts.NO_ARGS,
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
        paused = await asyncio.to_thread(self._companies.paused, company)
        refusal = acts.pause_refusal(company, paused)
        if refusal is not None:
            raise Invalid(refusal)
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
        paused = await asyncio.to_thread(self._companies.paused, company)
        refusal = acts.resume_refusal(company, paused)
        if refusal is not None:
            raise Invalid(refusal)
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
            acts.NO_ARGS,
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

    # -- what a human may do now ---------------------------------------------

    async def _preview(
        self,
        identity: Identity,
        act: str,
        args: dict[str, Any],
        company: str,
        run_id: str,
        refusal: str | None,
    ) -> ActView:
        """One act's preview: the state's refusal when the company's state
        refuses it — the act checks state first — else what policy would
        answer, asked exactly as the act asks it. Records nothing, does
        nothing."""
        decision = None
        if refusal is None:
            decision = await self._decision(identity, act, args, company, run_id)
        return views.act_view(act, args, refusal, decision)

    async def _previews(
        self,
        identity: Identity,
        company: str,
        plans: list[tuple[str, dict[str, Any], str, str | None]],
    ) -> list[ActView]:
        """Every `(act, args, run_id, state refusal)` previewed, policy's
        questions asked all at once."""
        return list(
            await asyncio.gather(
                *(
                    self._preview(identity, act, args, company, run_id, refusal)
                    for act, args, run_id, refusal in plans
                )
            )
        )

    async def acts(self, identity: Identity) -> ActList:
        """The company's acts as this person could ask for them now:
        apply, delete, wake, pause, resume, and a start per workflow a
        human may start. Policy's questions are asked all at once."""
        company = await self._company()
        paused, heartbeat = await asyncio.gather(
            asyncio.to_thread(self._companies.paused, company),
            self._execution(workflow_id("heartbeat", company)),
        )
        loop = heartbeat_run_id(company)
        plans = [
            (BLUEPRINT_APPLY, acts.apply_args(company), "", None),
            (COMPANY_DELETE, acts.NO_ARGS, "", None),
            (HEARTBEAT_WAKE, acts.NO_ARGS, loop, acts.wake_refusal(company, heartbeat)),
            (HEARTBEAT_PAUSE, acts.NO_ARGS, loop, acts.pause_refusal(company, paused)),
            (HEARTBEAT_RESUME, acts.NO_ARGS, loop, acts.resume_refusal(company, paused)),
            *(
                (RUN_START, acts.start_args(workflow, None), "", None)
                for workflow in HUMAN_STARTABLE
            ),
        ]
        return ActList(run_id=None, acts=await self._previews(identity, company, plans))

    async def run_acts(self, identity: Identity, run_id: str) -> ActList:
        """The run's acts as this person could ask for them now: cancel,
        and a verdict per gate its workflow has, approved or not."""
        record, wf_id = await asyncio.to_thread(self._run_of, run_id)
        execution = await self._execution(wf_id)
        plans = [
            (
                RUN_CANCEL,
                acts.cancel_args(run_id),
                run_id,
                acts.cancel_refusal(record, execution),
            )
        ]
        for gate in GATES_OF.get(record.workflow, ()):
            refusal = acts.verdict_refusal(record, gate, execution)
            plans += [
                (GATE_RESOLVE, acts.verdict_args(gate, run_id, approved), run_id, refusal)
                for approved in (True, False)
            ]
        answered = await self._previews(identity, record.company or "", plans)
        return ActList(run_id=run_id, acts=answered)

    async def now(self) -> Now:
        """The registry's clock — the fence a reader takes before acting so
        its reads are its own."""
        return Now(now=await asyncio.to_thread(self._clock))
