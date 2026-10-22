"""The console interface's models: one request and one response per
route, and the two error bodies.

Shaped in the glossary's terms, never in the registry's columns, a
workflow's internals or a CR's spelling: the console maps those onto
these in one place (`console.views`), so a column renamed behind the
interface changes the mapping, not a client. Every model forbids extra
fields — an internal field leaking through is an error at the console,
not a surprise in a client.

Timestamps are ISO 8601 with an offset; money is US dollars as a float.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from common.audit import RATIONALE_LIMIT
from common.console.enums import (
    AuditSource,
    CompanyPhase,
    Decision,
    DocumentKind,
    ExecutionStatus,
    Gate,
    HeartbeatState,
    RunState,
    RunStatus,
    StartableWorkflow,
    TickOutcome,
    Workflow,
)
from common.topology import BLUEPRINT_LIMIT_BYTES


class Model(BaseModel):
    """What every interface model shares: no field the interface does not
    name."""

    model_config = ConfigDict(extra="forbid")


# -- errors --------------------------------------------------------------------


class Problem(Model):
    """Why a request was not answered: 401 (no verified human), 404 (no
    company is applied), 422 (it names something that cannot be acted
    on, or a body that does not validate), 503 (Temporal unreachable)."""

    detail: str


class PolicyRefusal(Model):
    """A 403: policy refused the act, and `rule` names the rule."""

    detail: str
    rule: str


# -- requests ------------------------------------------------------------------

# A person's stated reason is part of the act: a verdict, a cancel, a
# pause, a resume or a delete without one is refused, and it is stored
# beside the act's audit row as its rationale.
Rationale = Field(min_length=1, max_length=RATIONALE_LIMIT)


class BlueprintRequest(Model):
    """`POST /blueprint/preview`, `POST /blueprint/apply`: the blueprint's
    YAML text, at most `BLUEPRINT_LIMIT_BYTES` bytes."""

    blueprint: str = Field(max_length=BLUEPRINT_LIMIT_BYTES)


class StartRunRequest(Model):
    """`POST /runs`: a run on a ticket (`implement`) or a pull request
    (`review`), by its number on the company repository."""

    workflow: StartableWorkflow
    number: int = Field(ge=1)


class VerdictRequest(Model):
    """`POST /runs/{run_id}/gates/{gate}`."""

    approved: bool
    rationale: str = Rationale


class CancelRequest(Model):
    """`POST /runs/{run_id}/cancel`."""

    rationale: str = Rationale


class PauseRequest(Model):
    """`POST /heartbeat/pause`."""

    rationale: str = Rationale


class ResumeRequest(Model):
    """`POST /heartbeat/resume`."""

    rationale: str = Rationale


class DeleteRequest(Model):
    """`DELETE /company`. `wait: false` answers before the reconciler's
    teardown ends; the act and the teardown are the same either way."""

    rationale: str = Rationale
    wait: bool = True


# -- the chokepoint ------------------------------------------------------------


class Health(Model):
    """`GET /healthz`: liveness and the release the console runs."""

    ok: bool
    version: str


class WhoAmI(Model):
    """`GET /whoami`: the verified human, and the company the cluster
    hosts (none before the first apply)."""

    human: str
    lifecycle: str
    company: str | None


class Now(Model):
    """`GET /now`: the registry's clock, the fence a reader takes before
    acting and passes back as `?since=`."""

    now: datetime


# -- the company ---------------------------------------------------------------


class PauseView(Model):
    """Who holds the heartbeat paused, and since when."""

    by: str
    at: datetime


class LimitsView(Model):
    """The charter's caps as they bind the company now, beside what they
    are measured against: the per-run cap, the daily cap and today's
    spend (since UTC midnight), how many runs may be live at once, and
    how long each gate waits for a human before it expires (it escalates
    at half)."""

    per_run_usd: float
    daily_cap_usd: float
    spent_today_usd: float
    max_concurrent_runs: int
    gate_deadline_minutes: dict[Gate, int]


class CompanyView(Model):
    """`GET /company`: the applied company as its Company CR says —
    `activating` until OPA serves its policy, `converged` after; the
    generation the phase answers for — its pause, and its limits (null
    until the company has converged once)."""

    company: str
    phase: CompanyPhase
    generation: int
    observed_generation: int
    bundle_revision: str
    message: str
    paused: PauseView | None
    limits: LimitsView | None


class BlueprintView(Model):
    """`GET /blueprint`: the blueprint's text exactly as a human applied
    it — applying it again changes nothing — who applied it and when, and
    whether policy enforces it yet: the Company's generation, the one its
    phase answers for, and the phase (`activating` until the text applied
    is the one policy enforces, `converged` after)."""

    company: str
    generation: int
    observed_generation: int
    phase: CompanyPhase
    text: str
    sha256: str
    applied_by: str
    applied_at: datetime


class DocumentView(Model):
    """`GET /documents/{kind}`: one of the company's two documents as it
    stands on the base ref, read live through the MCP gateway, clipped to
    what a model is shown of it. An absent document is `present: false`,
    not an error: a company's first ticks exist to write them."""

    kind: DocumentKind
    path: str
    ref: str
    present: bool
    text: str | None
    read_at: datetime


class BoundsView(Model):
    """`GET /company/bounds`: the charter's gates and limits as they bind
    the company's work, the text every dispatch carries into a
    procedure's system prompt."""

    company: str
    text: str


class StandingAgentView(Model):
    """One standing agent: its hire's name, its catalog role, whether the
    reconciler converged it, and whether it is the conductor the
    company's runs are requested under."""

    agent_id: str
    standing_name: str
    role: str
    status: str
    conductor: bool


class AgentList(Model):
    """`GET /agents`: the company's standing agents."""

    agents: list[StandingAgentView]


class TicketView(Model):
    """One ticket the company filed: its number on the repository, the
    run that filed it, when."""

    number: int
    run_id: str
    filed_at: datetime


class TicketList(Model):
    """`GET /tickets`: every ticket the company filed on its repository,
    ascending by number — the ones its agents and the platform filed,
    whatever the git host says of them since."""

    repo: str
    tickets: list[TicketView]


class PreviewResult(Model):
    """`POST /blueprint/preview`: what applying the blueprint would
    change, against what the cluster hosts. Nothing is written."""

    company: str
    diff: str


class ApplyResult(Model):
    """`POST /blueprint/apply`: the CRs created and updated, the
    generation this apply made (the one to wait `converged` for), and
    the rule the apply was allowed under."""

    company: str
    generation: int
    created: int
    updated: int
    diff: str
    rule: str


class CompanyDeleted(Model):
    """`DELETE /company`: the runs cancelled on the way, and how long the
    teardown took (0 without waiting)."""

    company: str
    runs_cancelled: list[str]
    elapsed_s: int
    rule: str


# -- runs ----------------------------------------------------------------------


class ExecutionView(Model):
    """A workflow's execution as Temporal tells it: its status, and while
    it runs, the state its `state()` query answers — a run parked on a
    gate reads `parked-<gate>`. `NOT_FOUND` when Temporal knows none."""

    workflow_id: str
    status: ExecutionStatus
    state: RunState | HeartbeatState | None


class ParkedView(Model):
    """Where a parked run waits: the gate, since when (its `parked` gate
    row), and when nobody answering escalates it (half the deadline) and
    expires it. The times are null when the park's row or the gate's
    deadline cannot be read."""

    gate: Gate
    deadline_minutes: int | None
    since: datetime | None
    escalates_at: datetime | None
    expires_at: datetime | None


class RunView(Model):
    """One run's record: its task, status and what it delivered, and the
    gate it is parked on, if any (null for a run that is not)."""

    run_id: str
    workflow: Workflow
    workflow_id: str
    company: str | None
    task: str
    status: RunStatus
    pr_url: str | None
    # The reviewer's verdict on a review run (`pass`, `fail`).
    verdict: str | None
    ticket_urls: list[str]
    iterations: int
    error: str | None
    created_at: datetime | None
    finished_at: datetime | None
    parked: ParkedView | None


class RunDetail(RunView):
    """`GET /runs/{run_id}`: the record and its workflow's execution."""

    execution: ExecutionView


class RunList(Model):
    """`GET /runs`: the company's runs, oldest first; with `?state=parked`,
    only those parked on a gate."""

    runs: list[RunView]


class EphemeralAgentView(Model):
    """One ephemeral agent bound to a run: its crew and role, and whether
    its identity is still active or was revoked at teardown."""

    agent_id: str
    crew: str
    role: str
    status: str


class CrewView(Model):
    """`GET /runs/{run_id}/crew`: every identity ever bound to the run."""

    run_id: str
    agents: list[EphemeralAgentView]


class RunStarted(Model):
    """`POST /runs`: the run id and the workflow addressing it. The run's
    record appears a moment later, when the worker writes it."""

    run_id: str
    workflow: StartableWorkflow
    workflow_id: str
    rule: str


class VerdictRecorded(Model):
    """`POST /runs/{run_id}/gates/{gate}`: the verdict recorded under the
    human's name, and delivered."""

    run_id: str
    gate: Gate
    approved: bool
    human: str
    rule: str


class RunCancelled(Model):
    """`POST /runs/{run_id}/cancel`: the workflow was asked to stop; its
    teardown compensates."""

    run_id: str
    workflow_id: str
    rule: str


# -- the audit story -----------------------------------------------------------


class AuditEventView(Model):
    """One audit row: who acted, through which source, on what, and what
    policy answered — with the actor's rationale when the row committed
    the company to something. The arguments are never in it."""

    id: int
    ts: datetime
    run_id: str
    agent_id: str
    role: str
    tool: str
    decision: Decision
    rule: str | None
    rationale: str | None
    latency_ms: int
    source: AuditSource
    company: str
    lifecycle: str
    parent_agent_id: str
    workflow: str


class StoryView(Model):
    """`GET /runs/{run_id}/story`: the run's rows in order, every source."""

    run_id: str
    events: list[AuditEventView]


class EventsSpend(Model):
    """The spend to date beside a page of events: the company's, and the
    run's when the page is narrowed to one."""

    usd: float
    run_usd: float | None


class EventsPage(Model):
    """`GET /events`: the company's rows above the cursor, in id order,
    and the cursor to continue from."""

    events: list[AuditEventView]
    next: int
    spend: EventsSpend


# -- spend ---------------------------------------------------------------------


class SpendView(Model):
    """`GET /spend`: the company's spend and tokens since a fence."""

    usd: float
    prompt_tokens: int
    completion_tokens: int


class SpendEntryView(Model):
    """One model call in the ledger, attributed."""

    ts: datetime
    run_id: str
    agent_id: str
    parent_agent_id: str
    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    usd: float


class RunSpendView(Model):
    """`GET /runs/{run_id}/spend`: the run's total and every call behind it."""

    run_id: str
    usd: float
    entries: list[SpendEntryView]


# -- the heartbeat -------------------------------------------------------------


class TickView(Model):
    """One heartbeat tick: what it ended on and why."""

    id: int
    ts: datetime
    outcome: TickOutcome
    detail: str


class HeartbeatView(Model):
    """`GET /heartbeat`: the standing loop's execution and its ticks since
    a fence, oldest first."""

    company: str
    execution: ExecutionView
    ticks: list[TickView]


class Woken(Model):
    """`POST /heartbeat/wake`: the loop was nudged to look now."""

    company: str
    workflow_id: str
    rule: str


class PauseState(Model):
    """`POST /heartbeat/pause`, `POST /heartbeat/resume`: who holds the
    loop now, or null once it is resumed."""

    company: str
    paused: PauseView | None
    rule: str
