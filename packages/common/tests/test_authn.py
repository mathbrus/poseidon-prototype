"""One authenticator, two doors.

The gateways verify workloads against the registry; the console verifies
humans against the names the deployment declares. The same TokenReview
serves both, with a different audience, so a token minted for one door is
worthless at the other — and every token a door refuses is audited under
an `authn_*` rule naming the principal it carried.
"""

import pytest

from common.audit import AuditEvent
from common.authn import (
    Authenticator,
    AuthMiddleware,
    Principal,
    ServiceAccount,
    WorkloadAuthnSettings,
)
from common.identity import (
    HUMAN_ROLE,
    LIFECYCLE_HUMAN,
    LIFECYCLE_PLATFORM,
    LIFECYCLE_STANDING,
)
from common.topology import CONSOLE_TOKEN_AUDIENCE, TOKEN_AUDIENCE

pytestmark = pytest.mark.anyio

PLATFORM_NS = "poseidon-platform"
COMPANY_NS = "hello-saas"


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
        if service_account == "implementer-1":
            return {
                "agent_id": "implementer-1",
                "role": "implementer",
                "company": COMPANY_NS,
                "namespace": COMPANY_NS,
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
AGENT = Principal.of(f"system:serviceaccount:{COMPANY_NS}:implementer-1")
OIDC = Principal.of("alice@example.com")

REVIEWS = {
    ("t-owner", CONSOLE_TOKEN_AUDIENCE): OWNER,
    ("t-stranger", CONSOLE_TOKEN_AUDIENCE): STRANGER,
    ("t-oidc", CONSOLE_TOKEN_AUDIENCE): OIDC,
    ("t-agent-at-console", CONSOLE_TOKEN_AUDIENCE): AGENT,
    ("t-worker", TOKEN_AUDIENCE): WORKER,
    ("t-agent", TOKEN_AUDIENCE): AGENT,
    ("t-oidc-at-gateway", TOKEN_AUDIENCE): OIDC,
    ("t-owner-at-gateway", TOKEN_AUDIENCE): OWNER,
}


@pytest.fixture
def settings() -> WorkloadAuthnSettings:
    return WorkloadAuthnSettings(platform_namespace=PLATFORM_NS)


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
    worker = await gateway.authenticate("Bearer t-worker", "/mcp")
    agent = await gateway.authenticate("Bearer t-agent", "/mcp")
    assert worker is not None and worker.lifecycle == LIFECYCLE_PLATFORM
    assert agent is not None and agent.lifecycle == LIFECYCLE_STANDING
    assert set(reviewer.audiences) == {TOKEN_AUDIENCE}


async def test_a_human_is_refused_at_the_gateway(gateway, audit):
    assert await gateway.authenticate("Bearer t-owner-at-gateway", "/mcp") is None
    assert audit.rules == [("authn_unknown_service_account", OWNER.username)]


async def test_an_identity_providers_user_is_refused_at_the_gateway(gateway, audit):
    assert await gateway.authenticate("Bearer t-oidc-at-gateway", "/mcp") is None
    assert audit.rules == [("authn_not_a_workload", "alice@example.com")]
    assert audit.events[0].source == "gateway"


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
