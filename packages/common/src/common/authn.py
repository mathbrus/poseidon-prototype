"""Identity verification, shared by the two gateways and the console.

One `Authenticator` resolves a bearer credential to an `Identity`. Every
credential is a token the Kubernetes API server issued, verified by
TokenReview against the verifier's own audience — so a workload's token
is worthless at the console and a human's token is worthless at a
gateway. What the verified principal maps to depends on which door it
arrived at:

  the gateways (workload mode) — the principal must be a ServiceAccount:
    platform ns / orchestrator SA -> platform lifecycle
    <company ns> / <agent_id>     -> the registry's `agent_instances` row
                                     (standing agents)
    <company ns> / ephemeral-*    -> a live `run_participants` row, looked
                                     up by ServiceAccount name (ephemeral
                                     agents); terminating the row revokes
                                     the identity on the next call, however
                                     long the token itself lives
    Either registry branch requires the row's recorded namespace to match
    the token's — the same SA name anywhere else is an impersonation
    attempt and is refused.

  the console (human mode) — the principal must be a person:
    platform ns / human-<name>    -> the human `<name>`, when the deployment
                                     declares them (a cluster without an
                                     identity provider)
    any non-ServiceAccount user   -> the human named by the API server's
                                     own authentication (OIDC)
    The registry is never consulted; whether that person may act on a
    company is compiled policy's question, asked by the console per act.

Every rejection of a *presented* token is audited (`authn_*` rules): a
token from the wrong service account, namespace or door must be visible
in the audit store, not just 401'd.
"""

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic_settings import BaseSettings

from common.audit import AuditEvent, Source, args_digest
from common.identity import (
    HUMAN_ROLE,
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_HUMAN,
    LIFECYCLE_PLATFORM,
    LIFECYCLE_STANDING,
    ORCHESTRATOR_ID,
    PLATFORM_ROLE,
    Identity,
    bearer_token,
)
from common.telemetry import get_logger
from common.topology import (
    HUMAN_SA_PREFIX,
    ORCHESTRATOR_SERVICE_ACCOUNT,
    TOKEN_AUDIENCE,
)

TOKENREVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"


class WorkloadAuthnSettings(BaseSettings):
    """The verification knobs every verifier shares; each service's settings
    class inherits this so one env layout configures them identically."""

    # The release namespace, set by every template.
    platform_namespace: str
    # Empty means "derive from the in-cluster environment"; unit tests leave
    # all three alone and inject a reviewer instead.
    kube_api_url: str = ""
    kube_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    kube_ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


# Successful TokenReviews are cached this long, so the API server sees one
# review per caller per minute, not one per call. The cache proves only
# "this token belongs to that principal" — the registry mapping
# (agent_instances or run_participants) is re-read on every call, so
# revocation is a registry write that bites immediately, for standing and
# ephemeral callers alike.
AUTHN_CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class ServiceAccount:
    namespace: str
    name: str

    @property
    def username(self) -> str:
        return f"system:serviceaccount:{self.namespace}:{self.name}"


@dataclass(frozen=True)
class Principal:
    """What TokenReview said: the username the API server authenticated,
    and — when that username is a ServiceAccount's — which one."""

    username: str
    service_account: ServiceAccount | None = None

    @classmethod
    def of(cls, username: str) -> "Principal":
        prefix, _, rest = username.partition(":serviceaccount:")
        if prefix == "system" and rest:
            namespace, _, name = rest.partition(":")
            if namespace and name:
                return cls(username, ServiceAccount(namespace=namespace, name=name))
        return cls(username)


class Reviewer(Protocol):
    async def review(self, token: str, audience: str) -> Principal | None: ...


class AuditSink(Protocol):
    def append(self, event: AuditEvent) -> None: ...


class InstanceReads(Protocol):
    """The registry's `agent_instances` reader: a standing caller's row by
    ServiceAccount name."""

    def get(self, service_account: str) -> dict[str, Any] | None: ...


class ParticipantReads(Protocol):
    """The registry's `run_participants` reader: an ephemeral caller's live
    binding row by ServiceAccount name."""

    def by_service_account(self, service_account: str) -> dict[str, Any] | None: ...


class KubeTokenReviewer:
    """TokenReview against the cluster's API server, authenticated with the
    verifier's own mounted service-account token (RBAC: system:auth-delegator).
    The API server checks signature, expiry, audience and that the
    ServiceAccount still exists — revoking an SA revokes its tokens."""

    def __init__(
        self,
        api_url: str,
        own_token_file: str,
        ca_file: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._own_token_file = own_token_file
        self._http = http_client or httpx.AsyncClient(
            base_url=api_url, verify=ca_file, timeout=10.0
        )

    async def review(self, token: str, audience: str) -> Principal | None:
        own_token = await asyncio.to_thread(
            lambda: open(self._own_token_file).read().strip()
        )
        resp = await self._http.post(
            TOKENREVIEW_PATH,
            headers={"Authorization": f"Bearer {own_token}"},
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [audience]},
            },
        )
        # Anything but a clean review answer fails closed: an unreachable
        # API server must never widen who the verifier trusts.
        resp.raise_for_status()
        status = resp.json().get("status") or {}
        if not status.get("authenticated"):
            return None
        username = (status.get("user") or {}).get("username", "")
        return Principal.of(username) if username else None


def default_reviewer(settings: WorkloadAuthnSettings) -> KubeTokenReviewer | None:
    """The in-cluster reviewer, or None outside a cluster (unit tests)."""
    api_url = settings.kube_api_url
    if not api_url:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if host:
            api_url = f"https://{host}:{port}"
    if not api_url or not os.path.exists(settings.kube_token_file):
        return None
    return KubeTokenReviewer(api_url, settings.kube_token_file, settings.kube_ca_file)


class Authenticator:
    """Bearer header in, Identity out; rejects audited on the way.

    Workload mode (the default, `humans=None`) maps ServiceAccounts through
    the registry. Human mode (`humans` = the names the deployment declares)
    maps people and never touches the registry; `instances` and
    `participants` are not consulted and may be None.
    """

    def __init__(
        self,
        settings: WorkloadAuthnSettings,
        audit: AuditSink,
        instances: InstanceReads | None = None,
        participants: ParticipantReads | None = None,
        reviewer: Reviewer | None = None,
        source: Source = "gateway",
        audience: str = TOKEN_AUDIENCE,
        humans: Iterable[str] | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._instances = instances
        self._participants = participants
        self._reviewer = reviewer
        self._source = source
        self._audience = audience
        self._humans = None if humans is None else frozenset(humans)
        if self._humans is None and (instances is None or participants is None):
            raise ValueError("workload mode needs the registry readers")
        self._log = get_logger("workload.authn")
        # token digest -> (principal, monotonic deadline); TokenReview
        # successes only. Deliberately *not* an identity cache: the cache
        # proves who holds the token, and the registry mapping runs on
        # every call so a row termination revokes without waiting out the
        # TTL. Bounded: expired entries are swept on insert.
        self._cache: dict[str, tuple[Principal, float]] = {}

    async def authenticate(self, authorization: str | None, path: str) -> Identity | None:
        token = bearer_token(authorization)
        if token is None:
            # No credential presented (probes, scanners) — a 401 with no
            # audit event; only rejected *tokens* are governance signals.
            return None
        return await self._verify_kube_token(token, path)

    # -- Kubernetes-issued tokens ------------------------------------------

    async def _verify_kube_token(self, token: str, path: str) -> Identity | None:
        if self._reviewer is None:
            await self._reject(path, "authn_unknown_token", "unknown")
            return None

        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None and cached[1] > time.monotonic():
            principal = cached[0]
        else:
            try:
                principal = await self._reviewer.review(token, self._audience)
            except Exception as exc:
                self._log.info("token review failed", extra={"error": str(exc)})
                await self._reject(path, "authn_review_unavailable", "unknown")
                return None
            if principal is None:
                await self._reject(path, "authn_token_rejected", "unknown")
                return None
            self._cache = {
                k: v for k, v in self._cache.items() if v[1] > time.monotonic()
            }
            self._cache[digest] = (
                principal,
                time.monotonic() + AUTHN_CACHE_TTL_S,
            )

        if self._humans is not None:
            return await self._map_human(principal, path)
        # Outside the cache on purpose: the registry rows are the live half
        # of the identity, and re-reading them per call is what makes
        # revocation a registry write rather than an expiry.
        if principal.service_account is None:
            await self._reject(path, "authn_not_a_workload", principal.username)
            return None
        return await self._map_service_account(principal.service_account, path)

    async def _map_human(self, principal: Principal, path: str) -> Identity | None:
        account = principal.service_account
        if account is None:
            # The API server authenticated a user of its own — an identity
            # provider's. The name is theirs; the authority is policy's.
            return Identity(
                agent_id=principal.username, role=HUMAN_ROLE, lifecycle=LIFECYCLE_HUMAN
            )
        name = account.name.removeprefix(HUMAN_SA_PREFIX)
        if (
            account.namespace != self._settings.platform_namespace
            or name == account.name
            or name not in self._humans
        ):
            # A workload's ServiceAccount, or a `human-*` the deployment
            # never declared: not a person this door knows.
            await self._reject(path, "authn_not_a_human", principal.username)
            return None
        return Identity(
            agent_id=name,
            role=HUMAN_ROLE,
            lifecycle=LIFECYCLE_HUMAN,
            namespace=account.namespace,
        )

    async def _map_service_account(
        self, account: ServiceAccount, path: str
    ) -> Identity | None:
        settings = self._settings
        if (
            account.namespace == settings.platform_namespace
            and account.name == ORCHESTRATOR_SERVICE_ACCOUNT
        ):
            # The orchestrator's privileged identity (platform
            # lifecycle); policy keys on the lifecycle via the platform
            # decision path.
            return Identity(
                agent_id=ORCHESTRATOR_ID,
                role=PLATFORM_ROLE,
                lifecycle=LIFECYCLE_PLATFORM,
            )
        # Standing first: instance ids never carry the crew SA prefix, so
        # the two lookups cannot shadow each other; the order only decides
        # which miss is checked first.
        row = await asyncio.to_thread(self._instances.get, account.name)
        if row is not None:
            if row.get("namespace") != account.namespace:
                await self._reject(path, "authn_namespace_mismatch", account.username)
                return None
            return Identity(
                agent_id=row["agent_id"],
                role=row["role"],
                company=row["company"],
                lifecycle=LIFECYCLE_STANDING,
                namespace=row.get("namespace"),
            )
        participant = await asyncio.to_thread(
            self._participants.by_service_account, account.name
        )
        if participant is None:
            await self._reject(path, "authn_unknown_service_account", account.username)
            return None
        if participant.get("namespace") != account.namespace:
            await self._reject(path, "authn_namespace_mismatch", account.username)
            return None
        # The row is the whole ephemeral identity: the catalog role and
        # the crew are what the compiled crew rules key on.
        return Identity(
            agent_id=participant["agent_id"],
            role=participant["role"],
            company=participant["company"],
            lifecycle=LIFECYCLE_EPHEMERAL,
            namespace=participant.get("namespace"),
            run_id=participant["run_id"],
            parent_agent_id=participant["parent_agent_id"],
            crew_template=participant["crew_template"],
        )

    # -- audited rejection --------------------------------------------------

    async def _reject(self, path: str, rule: str, principal: str) -> None:
        event = AuditEvent(
            run_id="",
            agent_id=principal,
            role="",
            tool=path,
            args_digest=args_digest({}),
            decision="deny",
            rule=rule,
            latency_ms=0,
            backend_status=None,
            # An authn reject is the chokepoint speaking about a
            # credential that mapped to nothing — no company, no lifecycle.
            # Each door names itself so its rejects are attributable.
            source=self._source,
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.info("authentication rejected", extra=event.model_dump())


class AuthMiddleware:
    """Authenticate every HTTP request once and stash the identity in the
    ASGI scope's state; 401 otherwise (rejected tokens are audited by the
    authenticator). Pure ASGI so every door — both gateways, the console —
    runs the identical check whatever framework hosts it. `open_paths` are
    the unauthenticated routes: health probes and nothing else."""

    def __init__(
        self,
        app: Any,
        authenticator: Authenticator,
        open_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.authenticator = authenticator
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self.open_paths:
            await self.app(scope, receive, send)
            return
        authorization = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name == b"authorization"
            ),
            None,
        )
        identity = await self.authenticator.authenticate(
            authorization, scope.get("path", "")
        )
        if identity is None:
            body = json.dumps({"detail": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["identity"] = identity
        await self.app(scope, receive, send)
