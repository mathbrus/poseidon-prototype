"""The console's routes: method, path, what each does, its request and
response models, and the decision an act must win (reads decide
nothing).

The console serves exactly this table — its tests hold the served set
equal to it — and the reference documentation renders it.
"""

from typing import NamedTuple

from pydantic import BaseModel

from common.console import models as m
from common.policy import (
    BLUEPRINT_APPLY,
    COMPANY_DELETE,
    GATE_RESOLVE,
    HEARTBEAT_PAUSE,
    HEARTBEAT_RESUME,
    HEARTBEAT_WAKE,
    RUN_CANCEL,
    RUN_START,
)


class Route(NamedTuple):
    method: str
    path: str
    summary: str
    response: type[BaseModel]
    request: type[BaseModel] | None = None
    decision: str = ""


ROUTES: tuple[Route, ...] = (
    Route(
        "GET",
        "/healthz",
        "liveness and the running version; the one unauthenticated route",
        m.Health,
    ),
    Route(
        "GET", "/whoami", "the verified human and the applied company, if any", m.WhoAmI
    ),
    Route(
        "POST",
        "/blueprint/preview",
        "compile against the live registry; the diff applying would make",
        m.PreviewResult,
        m.BlueprintRequest,
    ),
    Route(
        "POST",
        "/blueprint/apply",
        "compile and submit the Company and AgentInstance CRs",
        m.ApplyResult,
        m.BlueprintRequest,
        BLUEPRINT_APPLY,
    ),
    Route(
        "GET",
        "/blueprint",
        "the blueprint's text as a human applied it, who applied it, and whether"
        " policy enforces it yet",
        m.BlueprintView,
    ),
    Route(
        "GET",
        "/company",
        "the applied company's phase, generation and bundle revision from its CR,"
        " and its pause from the registry",
        m.CompanyView,
    ),
    Route(
        "GET",
        "/company/bounds",
        "the charter's gates and limits as every dispatch carries them",
        m.BoundsView,
    ),
    Route(
        "GET",
        "/documents/{kind}",
        "the strategy or the architecture masterplan, as it stands on the base ref",
        m.DocumentView,
    ),
    Route(
        "GET",
        "/agents",
        "the company's standing agents, the conductor marked",
        m.AgentList,
    ),
    Route(
        "GET",
        "/tickets",
        "every ticket the company filed on its repository",
        m.TicketList,
    ),
    Route(
        "DELETE",
        "/company",
        "stop the company: cancel its runs, delete its CR, wait for the teardown",
        m.CompanyDeleted,
        m.DeleteRequest,
        COMPANY_DELETE,
    ),
    Route(
        "POST",
        "/runs",
        "start one implement or review run by hand",
        m.RunStarted,
        m.StartRunRequest,
        RUN_START,
    ),
    Route(
        "GET",
        "/runs",
        "the company's runs since a fence (`?since=`); `?state=parked` keeps those"
        " parked on a gate, with their deadlines",
        m.RunList,
    ),
    Route(
        "GET",
        "/runs/{run_id}",
        "the run record and its workflow's execution",
        m.RunDetail,
    ),
    Route(
        "GET",
        "/runs/{run_id}/story",
        "the run's audit story, every source; `?decisions=true` keeps the rows"
        " carrying a rationale",
        m.StoryView,
    ),
    Route("GET", "/runs/{run_id}/spend", "the run's attributed spend", m.RunSpendView),
    Route(
        "GET",
        "/runs/{run_id}/crew",
        "every ephemeral agent bound to the run, active or revoked",
        m.CrewView,
    ),
    Route(
        "POST",
        "/runs/{run_id}/gates/{gate}",
        "record a verdict, then deliver it",
        m.VerdictRecorded,
        m.VerdictRequest,
        GATE_RESOLVE,
    ),
    Route(
        "POST",
        "/runs/{run_id}/cancel",
        "cancel a run; teardown compensates",
        m.RunCancelled,
        m.CancelRequest,
        RUN_CANCEL,
    ),
    Route(
        "GET",
        "/heartbeat",
        "the standing loop's execution and its ticks since a fence",
        m.HeartbeatView,
    ),
    Route(
        "POST",
        "/heartbeat/wake",
        "nudge the loop to look now",
        m.Woken,
        decision=HEARTBEAT_WAKE,
    ),
    Route(
        "POST",
        "/heartbeat/pause",
        "hold the loop: it starts nothing until resumed; runs in flight finish",
        m.PauseState,
        m.PauseRequest,
        HEARTBEAT_PAUSE,
    ),
    Route(
        "POST",
        "/heartbeat/resume",
        "lift the pause and wake the loop",
        m.PauseState,
        m.ResumeRequest,
        HEARTBEAT_RESUME,
    ),
    Route("GET", "/spend", "the company's spend since a fence", m.SpendView),
    Route(
        "GET",
        "/events",
        "the company's audit rows after a cursor (`?after=&run=`), with its spend to date",
        m.EventsPage,
    ),
    Route("GET", "/now", "the registry's clock: the fence to take before acting", m.Now),
    Route(
        "GET",
        "/acts",
        "what this human may do to the company now, and why not; a preview,"
        " never a decision",
        m.ActList,
    ),
    Route(
        "GET",
        "/runs/{run_id}/acts",
        "what this human may do to the run now, and why not; a preview, never a decision",
        m.ActList,
    ),
)


def route(method: str, path: str) -> Route:
    """The one route at `method path`."""
    for entry in ROUTES:
        if entry.method == method and entry.path == path:
            return entry
    raise KeyError(f"no route {method} {path}")
