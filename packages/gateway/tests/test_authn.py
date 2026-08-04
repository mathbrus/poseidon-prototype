"""Workload identity at the gateway (plan step 11).

Projected service-account tokens resolve through a (faked) TokenReview to
registry-backed identities; platform-signed ephemeral JWTs verify against
the shared key AND a live participants row; and every rejected token —
wrong service account, wrong namespace, dead participant, bad signature —
lands in the audit store under an `authn_*` rule, not just a 401.
"""

import json

import httpx
import pytest

from common import platformjwt
from common.authn import Authenticator, ServiceAccount
from gateway.config import GatewaySettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryRunParticipants,
)

from conftest import AGENT_TOKEN, RUNNER_TOKEN

pytestmark = pytest.mark.anyio

PLATFORM_NS = "poseidon-platform"
COMPANY_NS = "hello-saas"
JWT_KEY = "unit-test-platform-key"
AUD = "poseidon-gateway"
RUN_ID = "run-01AUTHN"


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
def authn_settings() -> GatewaySettings:
    return GatewaySettings(
        platform_namespace=PLATFORM_NS,
        platform_jwt_key=JWT_KEY,
        token_audience=AUD,
    )


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    store = InMemoryAgentInstances()
    store.upsert(
        "backend-engineer-1", COMPANY_NS, "backend-engineer",
        "backend-engineer", "standard", namespace=COMPANY_NS,
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
            "sa-agent": ServiceAccount(COMPANY_NS, "backend-engineer-1"),
            "sa-stranger": ServiceAccount("default", "default"),
            "sa-impostor": ServiceAccount("evil-ns", "backend-engineer-1"),
        }
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
    assert identity.agent_id == "backend-engineer-1"
    assert identity.role == "backend-engineer"
    assert identity.company == COMPANY_NS
    assert identity.lifecycle == "standing"
    assert identity.namespace == COMPANY_NS


async def test_unknown_service_account_is_rejected_and_audited(authenticator, audit):
    assert await authenticator.authenticate("Bearer sa-stranger", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]
    assert audit.events[0].agent_id == "system:serviceaccount:default:default"
    assert audit.events[0].decision == "deny"


async def test_same_sa_name_in_another_namespace_is_rejected(authenticator, audit):
    """The step-11 done-when: a token from the wrong namespace is refused
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
    lone = Authenticator(
        authn_settings, audit, instances, participants, reviewer=None
    )
    assert await lone.authenticate("Bearer sa-agent", "/mcp") is None
    assert rules(audit) == ["authn_unknown_token"]


# -- platform-signed ephemeral JWTs (§2b) -----------------------------------


def mint(_key: str = JWT_KEY, _ttl: int = 600, **overrides) -> str:
    claims = {
        "sub": "executor-01X",
        "company": COMPANY_NS,
        "role": "backend-engineer",
        "lifecycle": "ephemeral",
        "run_id": RUN_ID,
        "parent_agent_id": "pm-1",
        **overrides,
    }
    return platformjwt.mint(_key, claims, AUD, ttl_s=_ttl)


async def test_ephemeral_jwt_with_live_row_is_accepted(authenticator, participants):
    participants.add("executor-01X", RUN_ID, "pm-1", "feature-delivery",
                     "executor")
    identity = await authenticator.authenticate(f"Bearer {mint()}", "/mcp")
    assert identity is not None
    assert identity.lifecycle == "ephemeral"
    assert identity.agent_id == "executor-01X"
    assert identity.company == COMPANY_NS
    # The §5.1 refinement: role stays the catalog template (the claim);
    # the crew coordinates and parentage come from the live row.
    assert identity.role == "backend-engineer"
    assert identity.crew_template == "feature-delivery"
    assert identity.crew_role == "executor"
    assert identity.parent_agent_id == "pm-1"
    assert identity.run_id == RUN_ID


async def test_ephemeral_jwt_without_row_is_rejected(authenticator, audit):
    """Signature alone must never be enough — no row, no identity."""
    assert await authenticator.authenticate(f"Bearer {mint()}", "/mcp") is None
    assert rules(audit) == ["authn_no_live_participant"]


async def test_row_termination_revokes_before_ttl(authenticator, participants, audit):
    participants.add("executor-01X", RUN_ID, "pm-1", "feature-delivery",
                     "backend-engineer")
    token = mint()
    assert await authenticator.authenticate(f"Bearer {token}", "/mcp") is not None

    participants.terminate("executor-01X")
    assert await authenticator.authenticate(f"Bearer {token}", "/mcp") is None
    assert rules(audit) == ["authn_no_live_participant"]


async def test_run_id_mismatch_is_rejected(authenticator, participants, audit):
    participants.add("executor-01X", "run-OTHER", "pm-1", "feature-delivery",
                     "backend-engineer")
    assert await authenticator.authenticate(f"Bearer {mint()}", "/mcp") is None
    assert rules(audit) == ["authn_no_live_participant"]


async def test_forged_signature_is_rejected_and_audited(authenticator, audit):
    forged = mint(_key="wrong-key")
    assert await authenticator.authenticate(f"Bearer {forged}", "/mcp") is None
    assert rules(audit) == ["authn_bad_signature"]


async def test_expired_jwt_is_rejected(authenticator, participants, audit):
    participants.add("executor-01X", RUN_ID, "pm-1", "feature-delivery",
                     "backend-engineer")
    stale = mint(_ttl=-10)
    assert await authenticator.authenticate(f"Bearer {stale}", "/mcp") is None
    assert rules(audit) == ["authn_expired"]


async def test_platform_jwt_path_disabled_without_key(
    authn_settings, audit, instances, participants, reviewer
):
    no_key = authn_settings.model_copy(update={"platform_jwt_key": ""})
    lone = Authenticator(no_key, audit, instances, participants, reviewer=reviewer)
    assert await lone.authenticate(f"Bearer {mint()}", "/mcp") is None
    assert rules(audit) == ["authn_platform_jwt_disabled"]


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
            return httpx.Response(201, json={"status": {
                "authenticated": True,
                "user": {"username":
                         "system:serviceaccount:hello-saas:backend-engineer-1"},
            }})
        return httpx.Response(201, json={"status": {"authenticated": False}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    reviewer = KubeTokenReviewer("https://kube.test", str(own), "unused-ca",
                                 http_client=client)
    account = await reviewer.review("good", AUD)
    assert account == ServiceAccount("hello-saas", "backend-engineer-1")
    assert await reviewer.review("bad", AUD) is None
    assert len(seen) == 2


async def test_kube_token_reviewer_ignores_non_service_accounts(tmp_path):
    """A human kubeconfig user authenticates fine at the API server but is
    not a workload; the gateway must not map it to anything."""
    from common.authn import KubeTokenReviewer

    own = tmp_path / "token"
    own.write_text("t")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"status": {
            "authenticated": True, "user": {"username": "kubernetes-admin"},
        }})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    reviewer = KubeTokenReviewer("https://kube.test", str(own), "ca",
                                 http_client=client)
    assert await reviewer.review("any", AUD) is None
