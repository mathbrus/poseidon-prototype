"""One authenticator, two chokepoints.

The gateways verify workloads against the registry; the console verifies
humans against the names the deployment declares. The same TokenReview
serves both, with a different audience, so a token minted for one is
worthless at the other — and every token either refuses is audited under
an `authn_*` rule naming the principal it carried.
"""

import pytest

from common.audit import AuditEvent
from common.authn import (
    Authenticator,
    AuthMiddleware,
    AuthnSettings,
    Principal,
    ServiceAccount,
)
from common.identity import (
    HUMAN_ROLE,
    LIFECYCLE_CONSOLE,
    LIFECYCLE_HUMAN,
    LIFECYCLE_PLATFORM,
    LIFECYCLE_STANDING,
    ORCHESTRATOR_ID,
    PLATFORM_ROLE,
)
from common.topology import (
    COMPANY_NAMESPACE,
    CONSOLE_TOKEN_AUDIENCE,
    PLATFORM_NAMESPACE,
    TOKEN_AUDIENCE,
)

pytestmark = pytest.mark.anyio

PLATFORM_NS = PLATFORM_NAMESPACE
COMPANY = "hello-saas"
# The company's standing conductor.
CONDUCTOR = "pm-1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Audit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    @property
    def rules(self) -> list[tuple[str, str]]:
        return [(e.rule or "", e.agent_id) for e in self.events]


class Instances:
    def get(self, service_account: str):
        if service_account == CONDUCTOR:
            return {
                "agent_id": CONDUCTOR,
                "role": "pm",
                "company": COMPANY,
                "namespace": COMPANY_NAMESPACE,
            }
        return None


class Participants:
    def by_service_account(self, service_account: str):
        return None


class Reviewer:
    """A scripted TokenReview: token -> principal, per audience."""

    def __init__(self, principals: dict[tuple[str, str], Principal]) -> None:
        self.principals = principals
        self.audiences: list[str] = []

    async def review(self, token: str, audience: str) -> Principal | None:
        self.audiences.append(audience)
        return self.principals.get((token, audience))


OWNER = Principal.of(f"system:serviceaccount:{PLATFORM_NS}:human-owner")
STRANGER = Principal.of(f"system:serviceaccount:{PLATFORM_NS}:human-stranger")
WORKER = Principal.of(f"system:serviceaccount:{PLATFORM_NS}:orchestrator-worker")
CONSOLE = Principal.of(f"system:serviceaccount:{PLATFORM_NS}:console")
AGENT = Principal.of(f"system:serviceaccount:{COMPANY_NAMESPACE}:{CONDUCTOR}")
OIDC = Principal.of("alice@example.com")

REVIEWS = {
    ("t-owner", CONSOLE_TOKEN_AUDIENCE): OWNER,
    ("t-stranger", CONSOLE_TOKEN_AUDIENCE): STRANGER,
    ("t-oidc", CONSOLE_TOKEN_AUDIENCE): OIDC,
    ("t-agent-at-console", CONSOLE_TOKEN_AUDIENCE): AGENT,
    ("t-worker", TOKEN_AUDIENCE): WORKER,
    ("t-console", TOKEN_AUDIENCE): CONSOLE,
    ("t-agent", TOKEN_AUDIENCE): AGENT,
    ("t-oidc-at-gateway", TOKEN_AUDIENCE): OIDC,
    ("t-owner-at-gateway", TOKEN_AUDIENCE): OWNER,
}


@pytest.fixture
def settings() -> AuthnSettings:
    return AuthnSettings(platform_namespace=PLATFORM_NS)


@pytest.fixture
def audit() -> Audit:
    return Audit()


@pytest.fixture
def reviewer() -> Reviewer:
    return Reviewer(REVIEWS)


@pytest.fixture
def console(settings, audit, reviewer) -> Authenticator:
    return Authenticator(
        settings,
        audit,
        reviewer=reviewer,
        source="console",
        audience=CONSOLE_TOKEN_AUDIENCE,
        humans=["owner"],
    )


@pytest.fixture
def gateway(settings, audit, reviewer) -> Authenticator:
    return Authenticator(settings, audit, Instances(), Participants(), reviewer=reviewer)


def test_a_principal_knows_whether_it_is_a_service_account():
    assert OWNER.service_account == ServiceAccount(PLATFORM_NS, "human-owner")
    assert OIDC.service_account is None
    assert Principal.of("system:serviceaccount:broken").service_account is None


async def test_a_declared_human_maps_by_name(console, reviewer):
    identity = await console.authenticate("Bearer t-owner", "/whoami")
    assert identity is not None
    assert (identity.agent_id, identity.role, identity.lifecycle) == (
        "owner",
        HUMAN_ROLE,
        LIFECYCLE_HUMAN,
    )
    assert identity.company is None
    assert reviewer.audiences == [CONSOLE_TOKEN_AUDIENCE]


async def test_an_identity_providers_user_maps_by_username(console):
    identity = await console.authenticate("Bearer t-oidc", "/whoami")
    assert identity is not None
    assert identity.agent_id == "alice@example.com"
    assert identity.lifecycle == LIFECYCLE_HUMAN


async def test_an_undeclared_human_is_refused_and_audited(console, audit):
    assert await console.authenticate("Bearer t-stranger", "/whoami") is None
    assert audit.rules == [("authn_not_a_human", STRANGER.username)]
    assert audit.events[0].source == "console"


async def test_a_workload_is_refused_at_the_console(console, audit):
    assert await console.authenticate("Bearer t-agent-at-console", "/runs") is None
    assert audit.rules == [("authn_not_a_human", AGENT.username)]


async def test_a_workloads_token_does_not_verify_for_the_console_audience(console, audit):
    # The reviewer only knows this token under the gateway audience.
    assert await console.authenticate("Bearer t-agent", "/runs") is None
    assert audit.rules == [("authn_token_rejected", "unknown")]


async def test_the_gateway_maps_workloads_through_the_registry(gateway, reviewer):
    """The orchestrator's ServiceAccount is the platform identity; a
    standing agent's is its `agent_instances` row, namespace included."""
    worker = await gateway.authenticate("Bearer t-worker", "/mcp")
    agent = await gateway.authenticate("Bearer t-agent", "/mcp")
    assert worker is not None
    assert (worker.agent_id, worker.role, worker.lifecycle) == (
        ORCHESTRATOR_ID,
        PLATFORM_ROLE,
        LIFECYCLE_PLATFORM,
    )
    assert agent is not None
    assert (agent.agent_id, agent.role, agent.company, agent.lifecycle) == (
        CONDUCTOR,
        "pm",
        COMPANY,
        LIFECYCLE_STANDING,
    )
    assert agent.namespace == COMPANY_NAMESPACE
    assert set(reviewer.audiences) == {TOKEN_AUDIENCE}


async def test_a_human_is_refused_at_the_gateway(gateway, audit):
    assert await gateway.authenticate("Bearer t-owner-at-gateway", "/mcp") is None
    assert audit.rules == [("authn_unknown_service_account", OWNER.username)]
    assert audit.events[0].decision == "deny"


class Unreachable:
    """A TokenReview that never answers: the API server is down, or the
    verifier lost its way to it."""

    async def review(self, token: str, audience: str) -> Principal | None:
        raise ConnectionError("kube-apiserver unreachable")


async def test_an_unreachable_token_review_refuses_the_caller(settings, audit, reviewer):
    """When TokenReview cannot answer, the token is refused and audited as
    `authn_review_unavailable`: an API server that is down never widens
    who the verifier trusts. The same token verifies once it answers."""
    down = Authenticator(
        settings, audit, Instances(), Participants(), reviewer=Unreachable()
    )
    assert await down.authenticate("Bearer t-agent", "/mcp") is None
    assert audit.rules == [("authn_review_unavailable", "unknown")]
    assert audit.events[0].decision == "deny"

    up = Authenticator(settings, audit, Instances(), Participants(), reviewer=reviewer)
    assert await up.authenticate("Bearer t-agent", "/mcp") is not None


async def test_an_identity_providers_user_is_refused_at_the_gateway(gateway, audit):
    assert await gateway.authenticate("Bearer t-oidc-at-gateway", "/mcp") is None
    assert audit.rules == [("authn_not_a_workload", "alice@example.com")]
    assert audit.events[0].source == "gateway"


class Companies:
    def applied(self) -> str | None:
        return COMPANY


async def test_the_console_reads_as_the_console_at_the_mcp_gateway_only(
    settings, audit, reviewer, gateway
):
    """The console's own ServiceAccount is the console identity — its own
    lifecycle, for the applied company, never the platform's — where the
    gateway passes the registry's companies (the MCP gateway); anywhere
    else it is a ServiceAccount the registry does not know."""
    mcp = Authenticator(
        settings, audit, Instances(), Participants(), Companies(), reviewer=reviewer
    )
    identity = await mcp.authenticate("Bearer t-console", "/mcp")
    assert identity is not None
    assert (identity.agent_id, identity.role, identity.lifecycle, identity.company) == (
        "console",
        "console",
        LIFECYCLE_CONSOLE,
        COMPANY,
    )
    assert await gateway.authenticate("Bearer t-console", "/chat") is None
    assert audit.rules == [("authn_unknown_service_account", CONSOLE.username)]


def test_workload_mode_needs_the_registry_readers(settings, audit):
    with pytest.raises(ValueError):
        Authenticator(settings, audit)


async def test_the_middleware_answers_401_and_leaves_open_paths_alone(console):
    calls: list[str] = []
    sent: list[dict] = []

    async def app(scope, receive, send):
        calls.append(scope["path"])
        calls.append(
            scope.get("state", {}).get("identity").agent_id
            if scope.get("state", {}).get("identity")
            else ""
        )

    async def send(message):
        sent.append(message)

    middleware = AuthMiddleware(app, console, open_paths=("/healthz",))
    await middleware({"type": "http", "path": "/healthz", "headers": []}, None, send)
    await middleware(
        {
            "type": "http",
            "path": "/whoami",
            "headers": [(b"authorization", b"Bearer t-owner")],
        },
        None,
        send,
    )
    await middleware({"type": "http", "path": "/whoami", "headers": []}, None, send)
    assert calls == ["/healthz", "", "/whoami", "owner"]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [401]
