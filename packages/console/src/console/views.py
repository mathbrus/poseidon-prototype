"""The one translation layer: registry rows, run records, Temporal's
answers and the Company CR, mapped onto the console interface.

Nothing else in the console knows both sides. A registry column renamed,
a Temporal field moved or a CR key respelled changes a mapper here, and
the interface the clients read stays what it was — or the mapper fails
its test, in the same pull request.
"""

from datetime import datetime
from typing import Any

from blueprint.bounds import render_bounds
from common.audit import HEARTBEAT_TICK, RATIONALE_EVENTS
from common.console import (
    EXECUTION_NOT_FOUND,
    ActRefusal,
    ActView,
    AgentList,
    AuditEventView,
    BlueprintView,
    BoundsView,
    CompanyView,
    CrewView,
    DocumentView,
    EphemeralAgentView,
    EventsPage,
    EventsSpend,
    ExecutionView,
    HeartbeatView,
    LimitsView,
    ParkedView,
    PauseView,
    RefusalKind,
    RunDetail,
    RunSpendView,
    RunView,
    SpendEntryView,
    SpendView,
    StandingAgentView,
    StoryView,
    TicketList,
    TicketView,
    TickView,
)
from common.text import clip_head
from common.topology import PHASE_ACTIVATING
from common.workflows import (
    BUDGET_GATE,
    MERGE_GATE,
    PARKED_STATE,
    gate_timer,
    parse_tick_rule,
    workflow_id,
)
from console.acts import policy_detail, refused_rule
from registry import RunRecord

# The gate a parked state names.
_GATE_OF_STATE = {state: gate for gate, state in PARKED_STATE.items()}


def pause_view(paused: dict[str, Any] | None) -> PauseView | None:
    """`Companies.paused`'s `{by, at}`, or None."""
    return PauseView(by=paused["by"], at=paused["at"]) if paused else None


def gate_deadlines(spec: dict[str, Any]) -> dict[str, int]:
    """How long each gate the applied spec declares waits for a human:
    the budget park's, and the review's merge gate when a human resolves
    it (a merge resolved by policy alone has no human deadline)."""
    deadlines = {BUDGET_GATE: int(spec["budgets"]["deadline_minutes"])}
    for config in spec.get("workflows") or []:
        merge = (config.get("gates") or {}).get(MERGE_GATE)
        if merge and merge.get("deadline_minutes") is not None:
            deadlines[MERGE_GATE] = int(merge["deadline_minutes"])
    return deadlines


def limits_view(spec: dict[str, Any], spent_today: float) -> LimitsView:
    """The applied spec's caps (`Companies.get`) and today's spend."""
    return LimitsView(
        per_run_usd=float(spec["budgets"]["per_run_usd"]),
        daily_cap_usd=float(spec["heartbeat"]["daily_cap_usd"]),
        spent_today_usd=spent_today,
        max_concurrent_runs=int(spec["heartbeat"]["max_concurrent_runs"]),
        gate_deadline_minutes=gate_deadlines(spec),
    )


def _cr_state(cr: dict[str, Any]) -> dict[str, Any]:
    """What every reader of the Company CR shows: its name, the generation
    applied, the one its phase answers for, and the phase — `activating`
    until the reconciler writes one."""
    status = cr.get("status") or {}
    return {
        "company": cr["metadata"]["name"],
        "generation": cr["metadata"].get("generation", 0),
        "observed_generation": status.get("observedGeneration", 0),
        "phase": status.get("phase") or PHASE_ACTIVATING,
    }


def blueprint_view(cr: dict[str, Any]) -> BlueprintView | None:
    """The blueprint's text the Company CR keeps (`spec.blueprintSource`),
    with the CR's generation and phase; None for a CR without one."""
    source = cr["spec"].get("blueprintSource")
    if not source:
        return None
    return BlueprintView(
        **_cr_state(cr),
        text=source["text"],
        sha256=source["sha256"],
        applied_by=source["appliedBy"],
        applied_at=source["appliedAt"],
    )


def document_view(
    kind: str, spec: dict[str, Any], read: dict[str, Any], read_at: str
) -> DocumentView:
    """`repo.read_file`'s `{present, text}` for one of the documents, the
    text clipped as the orchestrator clips what a model reads of it, so
    a person and a crew see the same picture."""
    present = bool(read.get("present"))
    text = read.get("text") if present else None
    return DocumentView(
        kind=kind,
        path=spec["documents"][kind],
        ref=spec["repo"]["base_ref"],
        present=present,
        text=clip_head(str(text)) if text is not None else None,
        read_at=read_at,
    )


def bounds_view(company: str, spec: dict[str, Any]) -> BoundsView:
    """The bounds, rendered from the applied spec the way the worker
    renders them for every dispatch."""
    return BoundsView(company=company, text=render_bounds(spec))


def company_view(
    cr: dict[str, Any], paused: dict[str, Any] | None, limits: LimitsView | None
) -> CompanyView:
    """The Company CR as `crs.read_company` reads it, the pause and the
    limits."""
    status = cr.get("status") or {}
    return CompanyView(
        **_cr_state(cr),
        bundle_revision=status.get("bundleRevision", ""),
        message=status.get("message", ""),
        paused=pause_view(paused),
        limits=limits,
    )


def run_workflow_id(record: RunRecord) -> str:
    """The workflow id that addresses a run: the one its row recorded,
    else the deterministic one."""
    return record.workflow_id or workflow_id(record.workflow, record.run_id)


def parked_gate(execution: ExecutionView) -> str | None:
    """The gate a run's execution says it is parked on, if any."""
    return _GATE_OF_STATE.get(str(execution.state)) if execution.state else None


def parked_view(gate: str, since: Any, deadline: int | None) -> ParkedView:
    """A park's deadlines, measured from its `parked` gate row (`since`,
    a registry timestamp) by the gate's own timer (`gate_timer`)."""
    if since is not None and not isinstance(since, datetime):
        since = datetime.fromisoformat(since)
    if since is None or deadline is None:
        escalates_at = expires_at = None
    else:
        escalate_after, expire_after = gate_timer(deadline)
        escalates_at, expires_at = since + escalate_after, since + expire_after
    return ParkedView(
        gate=gate,
        deadline_minutes=deadline,
        since=since,
        escalates_at=escalates_at,
        expires_at=expires_at,
    )


def run_view(record: RunRecord, parked: ParkedView | None = None) -> RunView:
    return RunView(
        run_id=record.run_id,
        workflow=record.workflow,
        workflow_id=run_workflow_id(record),
        company=record.company,
        task=record.task,
        status=record.status,
        pr_url=record.pr_url,
        verdict=record.verdict,
        ticket_urls=list(record.ticket_urls),
        iterations=record.iterations,
        error=record.error,
        created_at=record.created_at,
        finished_at=record.finished_at,
        parked=parked,
    )


def execution_view(execution: dict[str, Any]) -> ExecutionView:
    """`Workflows.inspect`'s answer."""
    return ExecutionView(
        workflow_id=execution["workflow_id"],
        status=execution["status"],
        state=execution.get("state"),
    )


def missing_execution(wf_id: str) -> ExecutionView:
    """The execution of a workflow Temporal does not know."""
    return ExecutionView(workflow_id=wf_id, status=EXECUTION_NOT_FOUND, state=None)


def run_detail(
    record: RunRecord, execution: ExecutionView, parked: ParkedView | None
) -> RunDetail:
    return RunDetail(**run_view(record, parked).model_dump(), execution=execution)


def crew_view(run_id: str, rows: list[dict[str, Any]]) -> CrewView:
    """`RunParticipants.for_run`: every identity bound to the run."""
    return CrewView(
        run_id=run_id,
        agents=[
            EphemeralAgentView(
                agent_id=row["agent_id"],
                crew=row["crew_template"],
                role=row["role"],
                status=row["status"],
            )
            for row in rows
        ],
    )


def agent_list(rows: list[dict[str, Any]], conductor: str | None) -> AgentList:
    """`AgentInstances.for_company`, the conductor named by its hire."""
    return AgentList(
        agents=[
            StandingAgentView(
                agent_id=row["agent_id"],
                standing_name=row["standing_name"],
                role=row["role"],
                status=row["status"],
                conductor=row["standing_name"] == conductor,
            )
            for row in rows
        ]
    )


def ticket_list(repo: str, rows: list[dict[str, Any]]) -> TicketList:
    """`FiledTickets.filed`, on the company's repository."""
    return TicketList(
        repo=repo,
        tickets=[
            TicketView(
                number=row["number"], run_id=row["run_id"], filed_at=row["filed_at"]
            )
            for row in rows
        ],
    )


def event_view(row: dict[str, Any]) -> AuditEventView:
    """One `audit_events` row; the digest of the arguments and the
    backend's status stay behind."""
    return AuditEventView(
        id=row["id"],
        ts=row["ts"],
        run_id=row["run_id"],
        agent_id=row["agent_id"],
        role=row["role"],
        tool=row["tool"],
        decision=row["decision"],
        rule=row["rule"],
        rationale=row["rationale"],
        latency_ms=row["latency_ms"],
        source=row["source"],
        company=row["company"],
        lifecycle=row["lifecycle"],
        parent_agent_id=row["parent_agent_id"],
        workflow=row["workflow"],
    )


def story_view(run_id: str, rows: list[dict[str, Any]]) -> StoryView:
    return StoryView(run_id=run_id, events=[event_view(row) for row in rows])


def events_page(
    rows: list[dict[str, Any]], after: int, usd: float, run_usd: float | None
) -> EventsPage:
    """A follower's page: the cursor moves to the last row's id, or stays."""
    return EventsPage(
        events=[event_view(row) for row in rows],
        next=rows[-1]["id"] if rows else after,
        spend=EventsSpend(usd=usd, run_usd=run_usd),
    )


def spend_view(total: dict[str, Any]) -> SpendView:
    """`BudgetLedger.spent_since`'s sums."""
    return SpendView(
        usd=total["usd"],
        prompt_tokens=total["prompt_tokens"],
        completion_tokens=total["completion_tokens"],
    )


def spend_entry_view(row: dict[str, Any]) -> SpendEntryView:
    """One `budget_ledger` row; its company is the console's own."""
    return SpendEntryView(
        ts=row["ts"],
        run_id=row["run_id"] or "",
        agent_id=row["agent_id"] or "",
        parent_agent_id=row["parent_agent_id"] or "",
        role=row["role"] or "",
        model=row["model"] or "",
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        cached_tokens=row["cached_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        usd=float(row["usd"]),
    )


def run_spend_view(
    run_id: str, usd: float, entries: list[dict[str, Any]]
) -> RunSpendView:
    return RunSpendView(
        run_id=run_id, usd=usd, entries=[spend_entry_view(row) for row in entries]
    )


def tick_view(row: dict[str, Any]) -> TickView:
    """A tick's row: its rule reads `tick_<outcome>:<detail>`."""
    outcome, detail = parse_tick_rule(str(row["rule"] or ""))
    return TickView(id=row["id"], ts=row["ts"], outcome=outcome, detail=detail)


def heartbeat_view(
    company: str, execution: ExecutionView, story: list[dict[str, Any]]
) -> HeartbeatView:
    """The loop's execution, and the tick rows of its story."""
    return HeartbeatView(
        company=company,
        execution=execution,
        ticks=[tick_view(row) for row in story if row["tool"] == HEARTBEAT_TICK],
    )


def act_view(
    act: str,
    args: dict[str, Any],
    refusal: str | None,
    decision: dict[str, Any] | None,
) -> ActView:
    """An act's preview: refused by the company's state (the act's 422
    detail; policy was not asked), or else allowed or refused by policy
    (the act's 403 rule)."""
    shown = {key: value for key, value in args.items() if value is not None}
    rule = refused_rule(decision) if decision is not None else None
    if refusal is not None:
        why = ActRefusal(kind=RefusalKind.state, rule=None, detail=refusal)
    elif rule is not None:
        why = ActRefusal(kind=RefusalKind.policy, rule=rule, detail=policy_detail(rule))
    else:
        why = None
    return ActView(
        act=act,
        args=shown,
        rationale_required=act in RATIONALE_EVENTS,
        allowed=why is None,
        refusal=why,
    )
