"""The LLM gateway pipeline against in-memory doubles
and a stubbed upstream: unattributed calls rejected, run attribution
verified, tier routing applied, every forwarded call metered to the ledger
and audited (source=llm-gateway), and the per-run cap a hard stop with a
distinguishable error."""

import asyncio

import httpx
import pytest

from common.authn import ServiceAccount
from gateway_llm.app import create_app
from gateway_llm.config import LlmGatewaySettings
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


class FakeReviewer:
    async def review(self, token: str, audience: str) -> ServiceAccount | None:
        if token == AGENT_TOKEN:
            return ServiceAccount(COMPANY, "backend-engineer-1")
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

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        self.requests.append(body)
        self.paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
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
                    },
                },
            )
        return httpx.Response(
            200,
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
                },
            },
        )


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def stores():
    audit = InMemoryAuditStore()
    ledger = InMemoryBudgetLedger()
    runs = InMemoryRuns()
    runs.start(RUN_ID, "task", "implement", f"implement-{RUN_ID}", COMPANY)
    instances = InMemoryAgentInstances()
    instances.upsert(
        "backend-engineer-1",
        COMPANY,
        "backend-engineer",
        "backend-engineer",
        "frontier",
        namespace=COMPANY,
    )
    participants = InMemoryRunParticipants()
    companies = InMemoryCompanies()
    companies.upsert(
        COMPANY,
        {
            "namespace": COMPANY,
            "budgets": {"per_run_usd": 5.0, "per_role_monthly_usd": {}},
        },
    )
    return audit, ledger, runs, instances, participants, companies


@pytest.fixture
def app(stores, upstream):
    audit, ledger, runs, instances, participants, companies = stores
    settings = LlmGatewaySettings(
        tier_models='{"frontier": "big-model", "standard": "small-model"}',
        default_model="fallback-model",
        price_per_1k_tokens=0.002,
    )
    return create_app(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(upstream.handler),
            base_url="http://upstream.test",
        ),
        audit=audit,
        ledger=ledger,
        runs=runs,
        instances=instances,
        participants=participants,
        companies=companies,
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


def test_unauthenticated_call_is_rejected(app):
    assert call(app, token=None).status_code == 401
    assert call(app, token="garbage").status_code == 401


def test_unattributed_call_is_rejected_and_audited(app, stores):
    audit = stores[0]
    resp = call(app, run_id=None)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "llm_unattributed"
    assert audit.events[-1].rule == "llm_unattributed"
    assert audit.events[-1].source == "llm-gateway"


def test_unknown_run_is_rejected(app, stores):
    resp = call(app, run_id="run-nobody-started")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "llm_unknown_run"


def test_tier_routes_the_model_and_meters_the_ledger(app, stores, upstream):
    audit, ledger = stores[0], stores[1]
    resp = call(app)
    assert resp.status_code == 200

    # The caller's frontier tier picked the model; the request's own model
    # field did not survive (routing is the gateway's, not the brain's).
    assert upstream.requests[0]["model"] == "big-model"

    (entry,) = ledger.entries_for_run(RUN_ID)
    assert entry["company"] == COMPANY
    assert entry["agent_id"] == "backend-engineer-1"
    assert entry["role"] == "backend-engineer"
    assert entry["model"] == "big-model"
    assert entry["prompt_tokens"] == 100 and entry["completion_tokens"] == 50
    assert abs(entry["usd"] - 0.0003) < 1e-9  # 150 tokens at 0.002/1K

    event = audit.events[-1]
    assert event.decision == "allow" and event.rule == "budget_headroom"
    assert event.source == "llm-gateway"
    assert event.company == COMPANY and event.workflow == "implement"


def test_exhausted_cap_hard_stops_with_a_distinguishable_error(app, stores):
    audit, ledger = stores[0], stores[1]
    ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="backend-engineer-1",
        parent_agent_id="",
        role="backend-engineer",
        model="big-model",
        prompt_tokens=1,
        completion_tokens=1,
        usd=5.0,
    )

    resp = call(app)
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
    ledger = stores[1]
    for _ in range(3):
        assert call(app).status_code == 200
    assert len(ledger.entries_for_run(RUN_ID)) == 3
    assert abs(ledger.spent_for_run(RUN_ID) - 0.0009) < 1e-9


def test_ephemeral_caller_cannot_bill_a_foreign_run(app, stores):
    """An ephemeral identity is bound to exactly one run; naming another is
    denied and audited before any budget or upstream work."""
    from common import platformjwt

    audit, _, runs, _, participants, _ = stores
    runs.start("run-OTHER", "task", "implement", None, COMPANY)
    participants.add("executor-01X", "run-OTHER", "pm-1", "feature-delivery", "executor")
    settings_key = "test-platform-key"
    # Rebuild the app with the JWT key enabled for this one test.
    token = platformjwt.mint(
        settings_key,
        {
            "sub": "executor-01X",
            "company": COMPANY,
            "role": "backend-engineer",
            "lifecycle": "ephemeral",
            "run_id": "run-OTHER",
        },
        "poseidon-gateway",
        ttl_s=600,
    )
    app_with_key = create_app(
        LlmGatewaySettings(platform_jwt_key=settings_key, default_model="fallback-model"),
        httpx.AsyncClient(
            transport=httpx.MockTransport(Upstream().handler),
            base_url="http://upstream.test",
        ),
        audit=audit,
        ledger=stores[1],
        runs=runs,
        instances=stores[3],
        participants=participants,
        companies=stores[5],
        token_reviewer=FakeReviewer(),
    )
    resp = call(app_with_key, token=token, run_id=RUN_ID)  # not its run
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "llm_foreign_run"
    assert audit.events[-1].rule == "llm_foreign_run"


def test_uncapped_company_is_not_blocked(app, stores, upstream):
    """A blueprint with no per-run cap meters without stopping — metering
    and enforcement are separable, silence is not."""
    companies = stores[5]
    companies.upsert(COMPANY, {"namespace": COMPANY, "budgets": {"per_run_usd": 0}})
    assert call(app).status_code == 200


# ---------------------------------------------------------------------------
# The OpenAI Responses surface: same chokepoint pipeline, different wire
# shape (SDKs auto-select it for models that list it, e.g. gpt-5*).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/responses", "/v1/responses"])
def test_responses_routes_the_tier_forwards_and_meters(app, stores, upstream, path):
    audit, ledger = stores[0], stores[1]
    resp = call(app, path=path)
    assert resp.status_code == 200
    assert resp.json()["object"] == "response"

    # Forwarded to the upstream Responses endpoint, with the tier's model —
    # the agent-chosen name never reaches upstream.
    assert upstream.paths[-1] == "/v1/responses"
    assert upstream.requests[-1]["model"] == "big-model"

    # input_tokens/output_tokens land on the ledger's prompt/completion
    # columns; the schema does not change per surface.
    (entry,) = ledger.entries_for_run(RUN_ID)
    assert entry["prompt_tokens"] == 100 and entry["completion_tokens"] == 50
    assert abs(entry["usd"] - 0.0003) < 1e-9

    event = audit.events[-1]
    assert event.decision == "allow" and event.rule == "budget_headroom"
    assert event.source == "llm-gateway" and event.tool == "llm.responses"


def test_responses_guards_match_chat_completions(app, stores):
    """No unauthenticated, unattributed or foreign-run passthrough opened up
    with the new surface."""
    assert call(app, token=None, path="/v1/responses").status_code == 401
    resp = call(app, run_id=None, path="/v1/responses")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "llm_unattributed"
    resp = call(app, run_id="run-nobody-started", path="/v1/responses")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "llm_unknown_run"


def test_responses_cap_is_the_same_hard_stop(app, stores):
    ledger = stores[1]
    ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="backend-engineer-1",
        parent_agent_id="",
        role="backend-engineer",
        model="big-model",
        prompt_tokens=1,
        completion_tokens=1,
        usd=5.0,
    )
    resp = call(app, path="/v1/responses")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "budget_exhausted"
    assert len(ledger.entries_for_run(RUN_ID)) == 1  # no new spend


def test_responses_digest_never_contains_prompt_text(app, stores, upstream):
    """A string `input` digests as one item; the audit event carries a hash,
    never content."""
    resp = call(
        app,
        path="/v1/responses",
        body={"model": "m", "input": "secret workspace contents"},
    )
    assert resp.status_code == 200
    event = stores[0].events[-1]
    assert "secret" not in event.args_digest
    assert len(event.args_digest) == 64  # sha256 hex, nothing else
