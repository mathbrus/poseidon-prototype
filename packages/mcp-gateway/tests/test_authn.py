"""Workload identity at the gateway, against the registry's own rows.

Every credential is a projected service-account token resolving through a
(faked) TokenReview to a registry-backed identity — standing agents via
`agent_instances`, ephemeral agents via a live `run_participants` row
looked up by ServiceAccount name. The TokenReview may be cached; the
registry mapping is re-read per call, so a row termination revokes
immediately. How the authenticator maps each kind of principal, and what
it refuses at which door, is proven in common's suite.
"""

import json

import httpx
import pytest
from conftest import (
    AGENT_ID,
    AGENT_TOKEN,
    COMPANY,
    CONDUCTOR,
    DELIVERY_CREW,
    PM_TOKEN,
    RUN_ID,
    ephemeral_id,
    ephemeral_service_account,
)

from common.authn import Authenticator, ServiceAccount
from common.topology import COMPANY_NAMESPACE, TOKEN_AUDIENCE
from registry.memory import InMemoryAuditStore

pytestmark = pytest.mark.anyio

AUD = TOKEN_AUDIENCE
CREW_SA = ephemeral_service_account(AGENT_ID)
# Another run's implementer: a ServiceAccount no live row binds.
UNBOUND_SA = ephemeral_service_account(
    ephemeral_id(DELIVERY_CREW, "implementer", "run-01OTHER")
)
CONDUCTOR_BEARER = f"Bearer {PM_TOKEN}"
CREW_BEARER = f"Bearer {AGENT_TOKEN}"


@pytest.fixture
def authenticator(settings, audit, instances, participants, reviewer):
    """The gateway's authenticator over the conftest's registry rows: the
    conductor and the run's ephemeral implementer, plus the tokens of
    callers no row maps — same-named ServiceAccounts in another namespace,
    and another run's implementer."""
    reviewer.accounts.update(
        {
            "sa-impostor": ServiceAccount("evil-ns", CONDUCTOR),
            "sa-crew-impostor": ServiceAccount("evil-ns", CREW_SA),
            "sa-unbound-crew": ServiceAccount(COMPANY_NAMESPACE, UNBOUND_SA),
        }
    )
    return Authenticator(settings, audit, instances, participants, reviewer=reviewer)


def rules(audit: InMemoryAuditStore) -> list[str]:
    return [e.rule for e in audit.events]


# -- projected service-account tokens --------------------------------------


async def test_same_sa_name_in_another_namespace_is_rejected(authenticator, audit):
    """The anti-impersonation contract: a token from the wrong namespace is refused
    even when the ServiceAccount name matches a real instance."""
    assert await authenticator.authenticate(CONDUCTOR_BEARER, "/mcp") is not None
    assert await authenticator.authenticate("Bearer sa-impostor", "/mcp") is None
    assert rules(audit) == ["authn_namespace_mismatch"]


async def test_missing_credential_is_401_but_not_audited(authenticator, audit):
    """Probes and scanners send no token; only presented tokens are
    governance signals."""
    assert await authenticator.authenticate(None, "/mcp") is None
    assert await authenticator.authenticate("Basic dXNlcg==", "/mcp") is None
    assert audit.events == []


async def test_token_review_is_cached(authenticator, reviewer):
    for _ in range(3):
        assert await authenticator.authenticate(CREW_BEARER, "/mcp")
    assert reviewer.reviews == 1


async def test_no_reviewer_fails_closed(settings, audit, instances, participants):
    lone = Authenticator(settings, audit, instances, participants, reviewer=None)
    assert await lone.authenticate(CREW_BEARER, "/mcp") is None
    assert rules(audit) == ["authn_unknown_token"]


# -- ephemeral crew ServiceAccounts ------------------------------------------


async def test_crew_sa_with_live_row_is_accepted(authenticator):
    identity = await authenticator.authenticate(CREW_BEARER, "/mcp")
    assert identity is not None
    assert identity.lifecycle == "ephemeral"
    # The row is the whole identity: the canonical agent_id (the SA name
    # is its lowercased, non-invertible projection), the catalog role, the
    # crew and the parentage.
    assert identity.agent_id == AGENT_ID
    assert identity.company == COMPANY
    assert identity.role == "implementer"
    assert identity.crew_template == DELIVERY_CREW
    assert identity.parent_agent_id == CONDUCTOR
    assert identity.run_id == RUN_ID
    assert identity.namespace == COMPANY_NAMESPACE


async def test_crew_sa_without_row_is_rejected(authenticator, audit):
    """A verified token alone must never be enough — no live row, no
    identity."""
    assert await authenticator.authenticate("Bearer sa-unbound-crew", "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


async def test_row_termination_revokes_before_ttl(authenticator, participants, audit):
    """The revocation-before-expiry guarantee, cache-shaped: the token is
    still valid and its TokenReview still cached, but the registry row is
    re-read on every call — terminating it kills the identity now."""
    assert await authenticator.authenticate(CREW_BEARER, "/mcp") is not None

    participants.terminate(AGENT_ID)
    assert await authenticator.authenticate(CREW_BEARER, "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


async def test_crew_sa_in_another_namespace_is_rejected(authenticator, audit):
    """The same anti-impersonation check standing agents get: a same-named
    ServiceAccount outside the row's recorded namespace maps to nothing."""
    assert await authenticator.authenticate("Bearer sa-crew-impostor", "/mcp") is None
    assert rules(audit) == ["authn_namespace_mismatch"]


async def test_standing_row_deletion_revokes_despite_a_warm_cache(
    authenticator, instances, audit
):
    """The cache proves token->SA only; firing a standing agent (its
    instance row deleted) bites on the very next call."""
    assert await authenticator.authenticate(CONDUCTOR_BEARER, "/mcp") is not None
    instances.delete(CONDUCTOR)
    assert await authenticator.authenticate(CONDUCTOR_BEARER, "/mcp") is None
    assert rules(audit) == ["authn_unknown_service_account"]


# -- the TokenReview wire format --------------------------------------------


async def test_kube_token_reviewer_speaks_tokenreview(tmp_path):
    from common.authn import KubeTokenReviewer

    own = tmp_path / "token"
    own.write_text("gateway-own-token\n")
    seen: list[httpx.Request] = []
    username = ServiceAccount(COMPANY_NAMESPACE, CREW_SA).username

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/apis/authentication.k8s.io/v1/tokenreviews"
        assert request.headers["authorization"] == "Bearer gateway-own-token"
        assert body["spec"]["audiences"] == [AUD]
        if body["spec"]["token"] == "good":
            return httpx.Response(
                201,
                json={"status": {"authenticated": True, "user": {"username": username}}},
            )
        return httpx.Response(201, json={"status": {"authenticated": False}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://kube.test"
    )
    reviewer = KubeTokenReviewer(
        "https://kube.test", str(own), "unused-ca", http_client=client
    )
    principal = await reviewer.review("good", AUD)
    assert principal is not None
    assert principal.service_account == ServiceAccount(COMPANY_NAMESPACE, CREW_SA)
    assert await reviewer.review("bad", AUD) is None
    assert len(seen) == 2


async def test_kube_token_reviewer_names_non_service_accounts(tmp_path):
    """A human kubeconfig user authenticates fine at the API server but is
    not a workload: the reviewer reports the principal by name (so the
    gateway's refusal is audited with it), and never as a ServiceAccount."""
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
    principal = await reviewer.review("any", AUD)
    assert principal is not None
    assert principal.username == "kubernetes-admin"
    assert principal.service_account is None
