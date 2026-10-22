"""`poseidon follow`: the ledger's rows as lines, the cursor as the
contract, spend only when it moves."""

from typing import Any

import httpx
from golden import golden
from typer.testing import CliRunner

from common.audit import HEARTBEAT_DECIDE, HEARTBEAT_TICK
from common.console import AuditEventView, EventsSpend
from poseidon_cli import ConsoleClient
from poseidon_cli import main as cli
from poseidon_cli.follow import follow, spend_line
from poseidon_cli.render import row as render

CONSOLE = "http://console.test"
# A gateway row as the console answers it, to vary.
GATEWAY_ROW = next(e for e in golden("GET /events")["events"] if e["source"] == "gateway")


def row(**overrides: Any) -> AuditEventView:
    return AuditEventView.model_validate(
        {
            **GATEWAY_ROW,
            "id": 1,
            "ts": "2026-09-11T12:04:10.123456Z",
            "run_id": "run-01J",
            "agent_id": "implementer-01J",
            "tool": "sandbox.run_tests",
            "rule": "implementer_sandbox",
            "rationale": None,
            **overrides,
        }
    )


def page(events: list[AuditEventView], next_id: int, **spend: Any) -> dict[str, Any]:
    """An `/events` answer, in the interface's shape."""
    return {
        "events": [e.model_dump(mode="json") for e in events],
        "next": next_id,
        "spend": EventsSpend(**{"run_usd": None, **spend}).model_dump(),
    }


def test_a_row_renders_as_one_line_naming_the_tool_and_the_decision():
    line = str(render(row()))
    assert line.startswith("12:04:10  run-01J")
    assert "implementer/implementer-01J" in line
    assert line.endswith("sandbox.run_tests  allow")  # an allow keeps its rule to itself


def test_a_denial_and_a_tick_say_their_rule():
    denied = str(render(row(decision="deny", rule="tool_not_in_scope")))
    assert denied.endswith("deny (tool_not_in_scope)")
    tick = str(
        render(
            row(
                run_id="heartbeat-hello-saas",
                agent_id="pm-1",
                role="pm",
                tool=HEARTBEAT_TICK,
                rule="max_concurrent_runs",
            )
        )
    )
    assert "  heartbeat  " in tick and tick.endswith("allow (max_concurrent_runs)")


def test_a_rationale_follows_its_row_on_its_own_line():
    """The one cleartext field: the actor's stated reason, under the row."""
    decided = str(
        render(
            row(
                run_id="heartbeat-hello-saas",
                tool=HEARTBEAT_DECIDE,
                rule="decision_valid",
                rationale="finish the open review before starting new work",
            )
        )
    )
    first, second = decided.split("\n")
    assert first.endswith("heartbeat.decide  allow (decision_valid)")
    assert second.strip() == "finish the open review before starting new work"
    assert "\n" not in str(render(row(rationale=None)))


def test_spend_prints_once_and_then_only_when_it_moves():
    def spend(usd: float, run_usd: float | None = None) -> EventsSpend:
        return EventsSpend(usd=usd, run_usd=run_usd)

    assert str(spend_line(spend(0.0), None)) == "spend   $0.00 total"
    assert spend_line(spend(0.0), 0.0) is None
    assert str(spend_line(spend(1.42), 1.38)) == "spend   $1.42 total  (+$0.04)"
    assert (
        str(spend_line(spend(1.42, 0.3), 1.38))
        == "spend   run $0.30 of $1.42 total  (+$0.04)"
    )


class ScriptedEvents:
    """A console answering /events page by page and /runs/{id} from a list."""

    def __init__(
        self, pages: list[dict[str, Any]], runs: list[dict[str, Any]] | None = None
    ):
        self.pages = list(pages)
        self.runs = list(runs or [])
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(
            str(request.url.path)
            + ("?" + request.url.query.decode() if request.url.query else "")
        )
        if request.url.path == "/events":
            answer = self.pages.pop(0) if self.pages else page([], 0, usd=0.0)
            return httpx.Response(200, json=answer)
        if request.url.path.startswith("/runs/"):
            return httpx.Response(200, json=self.runs.pop(0))
        if request.url.path == "/runs":
            return httpx.Response(
                200,
                json={
                    **golden("POST /runs"),
                    "run_id": "run-01J",
                    "workflow_id": "implement-run-01J",
                },
            )
        return httpx.Response(404, json={"detail": "no such route"})


def test_follow_prints_each_row_once_and_resumes_from_the_cursor():
    pages = [
        page([row(id=1), row(id=2, tool="git.open_pr")], 2, usd=0.0),
        page([row(id=3, decision="deny", rule="tool_not_in_scope")], 3, usd=0.5),
        page([], 3, usd=0.5),
    ]
    scripted = ScriptedEvents(pages)
    client = ConsoleClient(CONSOLE, "t", transport=httpx.MockTransport(scripted.handler))
    lines: list[str] = []
    polls = iter(range(3))

    cursor = follow(
        client,
        lambda line: lines.append(str(line)),
        stop=lambda: next(polls) == 2,
        sleep=lambda s: None,
    )

    assert cursor == 3
    assert [line for line in lines if line.startswith("12:04:10")] == [
        str(render(row(id=1))),
        str(render(row(id=2, tool="git.open_pr"))),
        str(render(row(id=3, decision="deny", rule="tool_not_in_scope"))),
    ]
    assert [line for line in lines if line.startswith("spend")] == [
        "spend   $0.00 total",
        "spend   $0.50 total  (+$0.50)",
    ]
    assert lines[-1].endswith("resume with --after 3")
    # Each poll asked from the last id it saw.
    assert scripted.asked == [
        "/events?after=0&limit=200",
        "/events?after=2&limit=200",
        "/events?after=3&limit=200",
    ]


def test_start_follows_its_own_rows_to_the_end(monkeypatch):
    done = {
        **golden("GET /runs/{run_id}"),
        "run_id": "run-01J",
        "status": "done",
        "workflow": "implement",
        "execution": {
            "workflow_id": "implement-run-01J",
            "status": "COMPLETED",
            "state": None,
        },
    }
    scripted = ScriptedEvents([page([row(id=7)], 7, usd=0.0, run_usd=0.0)], runs=[done])
    monkeypatch.setenv("POSEIDON_CONSOLE_URL", CONSOLE)
    monkeypatch.setenv("POSEIDON_TOKEN", "t")
    original = cli.ConsoleClient.__init__

    def patched(self, base_url=None, token=None, transport=None, timeout=120.0):
        original(self, base_url, token, httpx.MockTransport(scripted.handler), timeout)

    monkeypatch.setattr(cli.ConsoleClient, "__init__", patched)
    result = CliRunner().invoke(cli.app, ["start", "implement", "42", "--json"])
    assert result.exit_code == 0, result.output
    assert str(render(row(id=7))) in result.output and '"status": "done"' in result.output
    assert "/events?after=0&limit=200&run=run-01J" in scripted.asked
