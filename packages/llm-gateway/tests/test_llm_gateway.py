"""The LLM gateway pipeline against in-memory doubles
and a stubbed upstream: unattributed calls rejected, run attribution
verified, tier routing applied, every forwarded call metered to the ledger
and audited (source=llm-gateway), and the per-run cap a hard stop with a
distinguishable error."""

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from common.audit import args_digest
from common.authn import ServiceAccount
from llm_gateway.app import create_app
from llm_gateway.config import LlmGatewaySettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRunParticipants,
    InMemoryRuns,
)

pytestmark = pytest.mark.anyio

AGENT_TOKEN = "agent-token"
RUN_ID = "run-01BUDGET"
COMPANY = "hello-saas"


@pytest.fixture
def anyio_backend():
    return "asyncio"


CREW_TOKEN = "crew-secret"


class FakeReviewer:
    async def review(self, token: str, audience: str) -> ServiceAccount | None:
        if token == AGENT_TOKEN:
            return ServiceAccount(COMPANY, "implementer-1")
        if token == CREW_TOKEN:
            return ServiceAccount(COMPANY, "ephemeral-feature-delivery-implementer-01x")
        return None


class Upstream:
    """OpenAI-shaped upstream recording what was forwarded — serves both the
    chat-completions and the Responses surface, each with its own usage
    vocabulary, so the tests can prove which path the gateway forwarded to."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self.prompt_tokens = 100
        self.completion_tokens = 50
        self.cached_tokens = 50
        self.reasoning_tokens = 25
        self.cost = 0.0003
        self.send_cost_header = True
        self.cost_header_override: str | None = None

    def _headers(self) -> dict[str, str]:
        if not self.send_cost_header:
            return {}
        if self.cost_header_override is not None:
            return {"x-litellm-response-cost": self.cost_header_override}
        return {"x-litellm-response-cost": f"{self.cost:.10f}"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        self.requests.append(body)
        self.paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                headers=self._headers(),
                json={
                    "id": "up-resp-1",
                    "object": "response",
                    "created_at": 0,
                    "model": body["model"],
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg-1",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "hi", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": self.prompt_tokens,
                        "output_tokens": self.completion_tokens,
                        "total_tokens": self.prompt_tokens + self.completion_tokens,
                        "input_tokens_details": {"cached_tokens": self.cached_tokens},
                        "output_tokens_details": {
                            "reasoning_tokens": self.reasoning_tokens
                        },
                    },
                },
            )
        return httpx.Response(
            200,
            headers=self._headers(),
            json={
                "id": "up-1",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hi"},
                    }
                ],
                "usage": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "total_tokens": self.prompt_tokens + self.completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
                    "completion_tokens_details": {
                        "reasoning_tokens": self.reasoning_tokens
                    },
                },
            },
        )


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@dataclass
class Stores:
    audit: InMemoryAuditStore
    ledger: InMemoryBudgetLedger
    runs: InMemoryRuns
    instances: InMemoryAgentInstances
    participants: InMemoryRunParticipants
    companies: InMemoryCompanies


@pytest.fixture
def stores() -> Stores:
    audit = InMemoryAuditStore()
    ledger = InMemoryBudgetLedger()
    runs = InMemoryRuns()
    runs.start(RUN_ID, "task", "implement", f"implement-{RUN_ID}", COMPANY)
    instances = InMemoryAgentInstances()
    instances.upsert(
        "implementer-1",
        COMPANY,
        "implementer",
        "implementer",
        namespace=COMPANY,
    )
    participants = InMemoryRunParticipants()
    companies = InMemoryCompanies()
    companies.upsert(
        COMPANY,
        {
            "namespace": COMPANY,
            "budgets": {"per_run_usd": 5.0},
        },
    )
    return Stores(audit, ledger, runs, instances, participants, companies)


@pytest.fixture
def app(stores, upstream):
    settings = LlmGatewaySettings(model="big-model")
    return create_app(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler),
            base_url="http://upstream.test",
        ),
        audit=stores.audit,
        ledger=stores.ledger,
        runs=stores.runs,
        instances=stores.instances,
        participants=stores.participants,
        companies=stores.companies,
        token_reviewer=FakeReviewer(),
    )


def call(
    app,
    *,
    token: str | None = AGENT_TOKEN,
    run_id: str | None = RUN_ID,
    body: dict | None = None,
    path: str = "/chat/completions",
) -> httpx.Response:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if run_id:
        headers["X-Poseidon-Run-Id"] = run_id
    if body is None:
        body = (
            {
                "model": "whatever-the-agent-said",
                "instructions": "sys",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                    }
                ],
            }
            if path.endswith("/responses")
            else {
                "model": "whatever-the-agent-said",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://llm.test"
        ) as client:
            return await client.post(path, headers=headers, json=body)

    return asyncio.run(go())


# Every guard holds on both completion surfaces: same chokepoint, different
# wire shape.
SURFACES = ["/chat/completions", "/v1/responses"]


@pytest.mark.parametrize("path", SURFACES)
def test_unauthenticated_call_is_rejected(app, path):
    assert call(app, token=None, path=path).status_code == 401
    assert call(app, token="garbage", path=path).status_code == 401


@pytest.mark.parametrize("path", SURFACES)
def test_unattributed_call_is_rejected_and_audited(app, stores, path):
    audit = stores.audit
    resp = call(app, run_id=None, path=path)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "llm_unattributed"
    assert audit.events[-1].rule == "llm_unattributed"
    assert audit.events[-1].source == "llm-gateway"


@pytest.mark.parametrize("path", SURFACES)
def test_unknown_run_is_rejected(app, stores, path):
    resp = call(app, run_id="run-nobody-started", path=path)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "llm_unknown_run"


def test_the_deployment_model_is_pinned_and_metered(app, stores, upstream):
    audit, ledger = stores.audit, stores.ledger
    resp = call(app)
    assert resp.status_code == 200

    # The deployment's model answered; the request's own model field did
    # not survive (which model spends is the gateway's call, not the agent's).
    assert upstream.requests[0]["model"] == "big-model"

    (entry,) = ledger.entries_for_run(RUN_ID)
    assert entry["company"] == COMPANY
    assert entry["agent_id"] == "implementer-1"
    assert entry["role"] == "implementer"
    assert entry["model"] == "big-model"
    assert entry["prompt_tokens"] == 100 and entry["completion_tokens"] == 50
    assert entry["cached_tokens"] == 50 and entry["reasoning_tokens"] == 25
    assert entry["usd"] == upstream.cost  # verbatim from the cost header

    event = audit.events[-1]
    assert event.decision == "allow" and event.rule == "budget_headroom"
    assert event.source == "llm-gateway"
    assert event.company == COMPANY and event.workflow == "implement"


@pytest.mark.parametrize("path", SURFACES)
def test_exhausted_cap_hard_stops_with_a_distinguishable_error(app, stores, path):
    audit, ledger = stores.audit, stores.ledger
    ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="implementer-1",
        parent_agent_id="",
        role="implementer",
        model="big-model",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        reasoning_tokens=0,
        usd=5.0,
    )

    resp = call(app, path=path)
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["code"] == "budget_exhausted"
    assert "budget_exhausted" in error["message"]

    # No new spend, and the stop is a first-class audited governance event.
    assert len(ledger.entries_for_run(RUN_ID)) == 1
    event = audit.events[-1]
    assert event.decision == "deny" and event.rule == "budget_exhausted"
    assert event.source == "llm-gateway"


def test_cap_bites_only_at_the_cap(app, stores, upstream):
    """Below the cap calls flow and spend accrues call by call."""
    ledger = stores.ledger
    for _ in range(3):
        assert call(app).status_code == 200
    assert len(ledger.entries_for_run(RUN_ID)) == 3
    assert abs(ledger.spent_for_run(RUN_ID) - 3 * upstream.cost) < 1e-9


def test_ephemeral_caller_cannot_bill_a_foreign_run(app, stores):
    """An ephemeral identity is bound to exactly one run; naming another is
    denied and audited before any budget or upstream work."""
    audit, runs, participants = stores.audit, stores.runs, stores.participants
    runs.start("run-OTHER", "task", "implement", None, COMPANY)
    participants.add(
        "feature-delivery-implementer-01X",
        "run-OTHER",
        "pm-1",
        "feature-delivery",
        "implementer",
        company=COMPANY,
        namespace=COMPANY,
        service_account="ephemeral-feature-delivery-implementer-01x",
    )
    resp = call(app, token=CREW_TOKEN, run_id=RUN_ID)  # not its run
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "llm_foreign_run"
    assert audit.events[-1].rule == "llm_foreign_run"


def test_uncapped_company_is_not_blocked(app, stores, upstream):
    """A blueprint with no per-run cap meters without stopping — metering
    and enforcement are separable, silence is not."""
    companies = stores.companies
    companies.upsert(COMPANY, {"namespace": COMPANY, "budgets": {"per_run_usd": 0}})
    assert call(app).status_code == 200


@pytest.mark.parametrize("path", SURFACES)
def test_unpriced_completion_is_withheld_and_never_metered(app, stores, upstream, path):
    """Spend accrues from what the provider reports; a 200 with no cost
    header is a completion the gateway refuses to deliver — no payload,
    no ledger row, a deny on the audit stream."""
    audit, ledger = stores.audit, stores.ledger
    upstream.send_cost_header = False

    resp = call(app, path=path)
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "llm_cost_unreported"
    assert set(body) == {"error"}  # the upstream completion stayed withheld

    assert ledger.entries_for_run(RUN_ID) == []
    event = audit.events[-1]
    assert event.decision == "deny" and event.rule == "llm_cost_unreported"
    assert event.source == "llm-gateway"


@pytest.mark.parametrize("bad_cost", ["not-a-number", "-0.1", "nan"])
def test_garbled_cost_header_is_withheld_too(app, stores, upstream, bad_cost):
    """Zero is a price; missing, negative, or unparseable is not."""
    upstream.cost_header_override = bad_cost
    resp = call(app)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_cost_unreported"
    assert stores.ledger.entries_for_run(RUN_ID) == []


def test_zero_cost_is_a_price_and_is_recorded(app, stores, upstream):
    upstream.cost = 0.0
    assert call(app).status_code == 200
    (entry,) = stores.ledger.entries_for_run(RUN_ID)
    assert entry["usd"] == 0.0


# ---------------------------------------------------------------------------
# The OpenAI Responses surface: same chokepoint pipeline, different wire
# shape (SDKs auto-select it for models that list it, e.g. gpt-5*).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/responses", "/v1/responses"])
def test_responses_forwards_the_pinned_model_and_meters(app, stores, upstream, path):
    audit, ledger = stores.audit, stores.ledger
    resp = call(app, path=path)
    assert resp.status_code == 200
    assert resp.json()["object"] == "response"

    # Forwarded to the upstream Responses endpoint with the deployment's
    # model — the agent-chosen name never reaches upstream.
    assert upstream.paths[-1] == "/v1/responses"
    assert upstream.requests[-1]["model"] == "big-model"

    # input_tokens/output_tokens land on the ledger's prompt/completion
    # columns; the schema does not change per surface.
    (entry,) = ledger.entries_for_run(RUN_ID)
    assert entry["prompt_tokens"] == 100 and entry["completion_tokens"] == 50
    assert entry["cached_tokens"] == 50 and entry["reasoning_tokens"] == 25
    assert entry["usd"] == upstream.cost

    event = audit.events[-1]
    assert event.decision == "allow" and event.rule == "budget_headroom"
    assert event.source == "llm-gateway" and event.tool == "llm.responses"


def test_responses_digest_never_contains_prompt_text(app, stores, upstream):
    """A string `input` digests as one item; the audit event carries a hash,
    never content."""
    resp = call(
        app,
        path="/v1/responses",
        body={"model": "m", "input": "secret workspace contents"},
    )
    assert resp.status_code == 200
    event = stores.audit.events[-1]
    # The digest is over the request's *shape* — the model and how many
    # input items — never its text.
    assert event.args_digest == args_digest({"model": "m", "input": 1})
