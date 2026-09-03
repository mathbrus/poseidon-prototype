"""Workload identity at the gateway.

Every credential is a projected service-account token resolving through a
(faked) TokenReview to a registry-backed identity — standing agents via
`agent_instances`, ephemeral agents via a live `run_participants`
row looked up by ServiceAccount name. The TokenReview may be cached; the
registry mapping is re-read per call, so a row termination revokes
immediately. Every rejected token — wrong service account, wrong
namespace, dead participant — lands in the audit store under an
`authn_*` rule, not just a 401.
"""

import json

import httpx
import pytest

from common.authn import Authenticator, ServiceAccount
from common.topology import TOKEN_AUDIENCE
from mcp_gateway.config import McpGatewaySettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryRunParticipants,
)

pytestmark = pytest.mark.anyio

PLATFORM_NS = "poseidon-platform"
COMPANY_NS = "hello-saas"
AUD = TOKEN_AUDIENCE
RUN_ID = "run-01AUTHN"
CREW_ID = "feature-delivery-implementer-run-01AUTHN"
CREW_SA = f"ephemeral-{CREW_ID}".lower()


class FakeReviewer:
    """Scriptable TokenReview: token -> ServiceAccount, recording calls."""

    def __init__(self, accounts: dict[str, ServiceAccount]) -> None:
        self.accounts = accounts
        self.calls: list[tuple[str, str]] = []

    async def review(self, token: str, audience: str) -> ServiceAccount | None:
        self.calls.append((token, audience))
        if audience != AUD:
            return None
        return self.accounts.get(token)


@pytest.fixture
def authn_settings() -> McpGatewaySettings:
    return McpGatewaySettings(platform_namespace=PLATFORM_NS)


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    store = InMemoryAgentInstances()
    store.upsert(
        "implementer-1",
        COMPANY_NS,
        "implementer",
        "implementer",
        "standard",
        namespace=COMPANY_NS,
    )
    return store


@pytest.fixture
def participants() -> InMemoryRunParticipants:
    return InMemoryRunParticipants()


@pytest.fixture
def reviewer() -> FakeReviewer:
    return FakeReviewer(
        {
            "sa-orchestrator": ServiceAccount(PLATFORM_NS, "orchestrator-worker"),
            "sa-agent": ServiceAccount(COMPANY_NS, "implementer-1"),
            "sa-stranger": ServiceAccount("default", "default"),
            "sa-impostor": ServiceAccount("evil-ns", "implementer-1"),
            "sa-crew": ServiceAccount(COMPANY_NS, CREW_SA),
            "sa-crew-impostor": ServiceAccount("evil-ns", CREW_SA),
        }
    )


def bind_crew_row(participants, run_id: str = RUN_ID) -> None:
    participants.add(
        CREW_ID,
        run_id,
        "pm-1",
        "feature-delivery",
        "implementer",
        company=COMPANY_NS,
        namespace=COMPANY_NS,
        service_account=CREW_SA,
    )


@pytest.fixture
def authenticator(authn_settings, audit, instances, participants, reviewer):
    return Authenticator(
        authn_settings,
        audit,
        instances,
        participants,
        reviewer=reviewer,
    )


def rules(audit: InMemoryAuditStore) -> list[str]:
    return [e.rule for e in audit.events]


# -- projected service-account tokens --------------------------------------


async def test_orchestrator_token_maps_to_platform_lifecycle(authenticator):
    identity = await authenticator.authenticate("Bearer sa-orchestrator", "/mcp")
    assert identity is not None
    assert identity.lifecycle == "platform"
    assert identity.agent_id == "orchestrator"
    assert identity.role == "platform"


async def test_standing_agent_token_maps_through_the_registry(authenticator):
    identity = await authenticator.authenticate("Bearer sa-agent", "/mcp")
    assert identity is not None
    assert identity.agent_id == "implementer-1"
    assert identity.role == "implementer"
    assert identity.company == COMPANY_NS
    assert identity.lifecycle == "standing"
    assert identity.namespace == COMPANY_NS


async def test_unknown_service_account_is_rejected_and_audited(authenticator, audit):
    assert await authenticator.authenticate("Bearer sa-stranger", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]
    assert audit.events[0].agent_id == "system:serviceaccount:default:default"
    assert audit.events[0].decision == "deny"


async def test_rejects_carry_the_constructing_gateway_as_source(
    authn_settings, audit, instances, participants, reviewer
):
    """Each gateway names itself: an LLM-gateway authn reject must not be
    attributed to the MCP gateway in the audit store."""
    llm = Authenticator(
        authn_settings,
        audit,
        instances,
        participants,
        reviewer=reviewer,
        source="llm-gateway",
    )
    assert await llm.authenticate("Bearer sa-stranger", "/v1/completions") is None
    assert audit.events[0].source == "llm-gateway"


async def test_rejects_default_to_the_mcp_gateway_source(authenticator, audit):
    assert await authenticator.authenticate("Bearer sa-stranger", "/mcp") is None
    assert audit.events[0].source == "gateway"


async def test_same_sa_name_in_another_namespace_is_rejected(authenticator, audit):
    """The anti-impersonation contract: a token from the wrong namespace is refused
    even when the ServiceAccount name matches a real instance."""
    assert await authenticator.authenticate("Bearer sa-impostor", "/mcp") is None
    assert rules(audit) == ["authn_namespace_mismatch"]


async def test_unreviewable_garbage_is_rejected_and_audited(authenticator, audit):
    assert await authenticator.authenticate("Bearer nonsense", "/mcp") is None
    assert rules(audit) == ["authn_token_rejected"]


async def test_missing_credential_is_401_but_not_audited(authenticator, audit):
    """Probes and scanners send no token; only presented tokens are
    governance signals."""
    assert await authenticator.authenticate(None, "/mcp") is None
    assert await authenticator.authenticate("Basic dXNlcg==", "/mcp") is None
    assert audit.events == []


async def test_token_review_is_cached(authenticator, reviewer):
    for _ in range(3):
        assert await authenticator.authenticate("Bearer sa-agent", "/mcp")
    assert len(reviewer.calls) == 1


async def test_no_reviewer_fails_closed(authn_settings, audit, instances, participants):
    lone = Authenticator(authn_settings, audit, instances, participants, reviewer=None)
    assert await lone.authenticate("Bearer sa-agent", "/mcp") is None
    assert rules(audit) == ["authn_unknown_token"]


# -- ephemeral crew ServiceAccounts ------------------------------------------


async def test_crew_sa_with_live_row_is_accepted(authenticator, participants):
    bind_crew_row(participants)
    identity = await authenticator.authenticate("Bearer sa-crew", "/mcp")
    assert identity is not None
    assert identity.lifecycle == "ephemeral"
    # The row is the whole identity: the canonical agent_id (the SA name
    # is its lowercased, non-invertible projection), the catalog role, the
    # crew and the parentage.
    assert identity.agent_id == CREW_ID
    assert identity.company == COMPANY_NS
    assert identity.role == "implementer"
    assert identity.crew_template == "feature-delivery"
    assert identity.parent_agent_id == "pm-1"
    assert identity.run_id == RUN_ID
    assert identity.namespace == COMPANY_NS


async def test_crew_sa_without_row_is_rejected(authenticator, audit):
    """A verified token alone must never be enough — no live row, no
    identity."""
    assert await authenticator.authenticate("Bearer sa-crew", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


async def test_row_termination_revokes_before_ttl(authenticator, participants, audit):
    """The revocation-before-expiry guarantee, cache-shaped: the token is
    still valid and its TokenReview still cached, but the registry row is
    re-read on every call — terminating it kills the identity now."""
    bind_crew_row(participants)
    assert await authenticator.authenticate("Bearer sa-crew", "/mcp") is not None

    participants.terminate(CREW_ID)
    assert await authenticator.authenticate("Bearer sa-crew", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


async def test_crew_sa_in_another_namespace_is_rejected(
    authenticator, participants, audit
):
    """The same anti-impersonation check standing agents get: a same-named
    ServiceAccount outside the row's recorded namespace maps to nothing."""
    bind_crew_row(participants)
    assert await authenticator.authenticate("Bearer sa-crew-impostor", "/mcp") is None
    assert rules(audit) == ["authn_namespace_mismatch"]


async def test_standing_row_deletion_revokes_despite_a_warm_cache(
    authenticator, instances, audit
):
    """The cache proves token->SA only; firing a standing agent (its
    instance row deleted) bites on the very next call."""
    assert await authenticator.authenticate("Bearer sa-agent", "/mcp") is not None
    instances.rows.pop("implementer-1")
    assert await authenticator.authenticate("Bearer sa-agent", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


# -- the TokenReview wire format --------------------------------------------


async def test_kube_token_reviewer_speaks_tokenreview(tmp_path):
    from common.authn import KubeTokenReviewer

    own = tmp_path / "token"
    own.write_text("gateway-own-token\n")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/apis/authentication.k8s.io/v1/tokenreviews"
        assert request.headers["authorization"] == "Bearer gateway-own-token"
        assert body["spec"]["audiences"] == [AUD]
        if body["spec"]["token"] == "good":
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "user": {
                            "username": "system:serviceaccount:hello-saas:implementer-1"
                        },
                    }
                },
            )
        return httpx.Response(201, json={"status": {"authenticated": False}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    reviewer = KubeTokenReviewer(
        "https://kube.test", str(own), "unused-ca", http_client=client
    )
    account = await reviewer.review("good", AUD)
    assert account == ServiceAccount("hello-saas", "implementer-1")
    assert await reviewer.review("bad", AUD) is None
    assert len(seen) == 2


async def test_kube_token_reviewer_ignores_non_service_accounts(tmp_path):
    """A human kubeconfig user authenticates fine at the API server but is
    not a workload; the gateway must not map it to anything."""
    from common.authn import KubeTokenReviewer

    own = tmp_path / "token"
    own.write_text("t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "status": {
                    "authenticated": True,
                    "user": {"username": "kubernetes-admin"},
                }
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    reviewer = KubeTokenReviewer("https://kube.test", str(own), "ca", http_client=client)
    assert await reviewer.review("any", AUD) is None
