"""Workload identity verification, shared by
both gateways — the MCP gateway and the LLM gateway verify the same
credentials against the same registry state.

One `Authenticator` resolves a bearer credential to an `Identity`. Every
credential is a projected Kubernetes ServiceAccount token, verified by
TokenReview with our audience; the ServiceAccount then maps to an
identity through the registry:

  platform ns / orchestrator SA -> platform lifecycle
  <company ns> / <agent_id>     -> the registry's `agent_instances` row
                                   (standing agents)
  <company ns> / crew-*         -> a live `run_participants` row, looked
                                   up by ServiceAccount name (ephemeral
                                   crew members); terminating the row
                                   revokes the identity on the next call,
                                   however long the token itself lives

Either registry branch requires the row's recorded namespace to match the
token's — the same SA name anywhere else is an impersonation attempt and
is refused.

Every rejection of a *presented* token is audited (`authn_*` rules): a
token from the wrong service account or namespace must be visible in the
audit store, not just 401'd.
"""

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic_settings import BaseSettings

from common.audit import AuditEvent, Source, args_digest
from common.identity import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PLATFORM,
    LIFECYCLE_STANDING,
    ORCHESTRATOR_ID,
    PLATFORM_ROLE,
    Identity,
    bearer_token,
)
from common.telemetry import get_logger

TOKENREVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"


class WorkloadAuthnSettings(BaseSettings):
    """The verification knobs both gateways share; each gateway's settings
    class inherits this so one env layout configures them identically."""

    platform_namespace: str = "poseidon-platform"
    orchestrator_service_account: str = "orchestrator-worker"
    token_audience: str = "poseidon-gateway"
    # Empty means "derive from the in-cluster environment"; unit tests leave
    # all three alone and inject a reviewer instead.
    kube_api_url: str = ""
    kube_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    kube_ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    # Successful TokenReviews are cached this long, so the API server sees
    # one review per caller per minute, not one per call. The cache proves
    # only "this token belongs to that ServiceAccount" — the registry
    # mapping (agent_instances or run_participants) is re-read on every
    # call, so revocation is a registry write that bites immediately, for
    # standing and ephemeral callers alike.
    authn_cache_ttl_s: float = 60.0


@dataclass(frozen=True)
class ServiceAccount:
    namespace: str
    name: str

    @property
    def username(self) -> str:
        return f"system:serviceaccount:{self.namespace}:{self.name}"


class Reviewer(Protocol):
    async def review(self, token: str, audience: str) -> ServiceAccount | None: ...


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
    gateway's own mounted service-account token (RBAC: system:auth-delegator).
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
        self._log = get_logger("workload.authn")

    async def review(self, token: str, audience: str) -> ServiceAccount | None:
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
        # API server must never widen who the gateway trusts.
        resp.raise_for_status()
        status = resp.json().get("status") or {}
        if not status.get("authenticated"):
            return None
        username = (status.get("user") or {}).get("username", "")
        prefix, _, rest = username.partition(":serviceaccount:")
        if prefix != "system" or not rest:
            self._log.info(
                "token review: non-serviceaccount principal", extra={"username": username}
            )
            return None
        namespace, _, name = rest.partition(":")
        if not namespace or not name:
            return None
        return ServiceAccount(namespace=namespace, name=name)


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
    """Bearer header in, Identity out; rejects audited on the way."""

    def __init__(
        self,
        settings: WorkloadAuthnSettings,
        audit: AuditSink,
        instances: InstanceReads,
        participants: ParticipantReads,
        reviewer: Reviewer | None = None,
        source: Source = "gateway",
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._instances = instances
        self._participants = participants
        self._reviewer = reviewer
        self._source = source
        self._log = get_logger("workload.authn")
        # token digest -> (service account, monotonic deadline); TokenReview
        # successes only. Deliberately *not* an identity cache: the cache
        # proves who holds the token, and the registry mapping runs on
        # every call so a row termination revokes without waiting out the
        # TTL. Bounded: expired entries are swept on insert.
        self._cache: dict[str, tuple[ServiceAccount, float]] = {}

    async def authenticate(self, authorization: str | None, path: str) -> Identity | None:
        token = bearer_token(authorization)
        if token is None:
            # No credential presented (probes, scanners) — a 401 with no
            # audit event; only rejected *tokens* are governance signals.
            return None
        return await self._verify_kube_token(token, path)

    # -- projected service-account tokens ----------------------------------

    async def _verify_kube_token(self, token: str, path: str) -> Identity | None:
        if self._reviewer is None:
            await self._reject(path, "authn_unknown_token", "unknown")
            return None

        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None and cached[1] > time.monotonic():
            account = cached[0]
        else:
            try:
                account = await self._reviewer.review(
                    token, self._settings.token_audience
                )
            except Exception as exc:
                self._log.info("token review failed", extra={"error": str(exc)})
                await self._reject(path, "authn_review_unavailable", "unknown")
                return None
            if account is None:
                await self._reject(path, "authn_token_rejected", "unknown")
                return None
            self._cache = {
                k: v for k, v in self._cache.items() if v[1] > time.monotonic()
            }
            self._cache[digest] = (
                account,
                time.monotonic() + self._settings.authn_cache_ttl_s,
            )

        # Outside the cache on purpose: the registry rows are the live half
        # of the identity, and re-reading them per call is what makes
        # revocation a registry write rather than an expiry.
        return await self._map_service_account(account, path)

    async def _map_service_account(
        self, account: ServiceAccount, path: str
    ) -> Identity | None:
        settings = self._settings
        if (
            account.namespace == settings.platform_namespace
            and account.name == settings.orchestrator_service_account
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
                role=row["role_template"],
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
        # The row is the whole ephemeral identity: `role_template` is the
        # catalog template policy roles resolve to, `role` the crew member
        # name — the crew_template + crew_role pair the compiled crew
        # rules key on.
        return Identity(
            agent_id=participant["agent_id"],
            role=participant["role_template"],
            company=participant["company"],
            lifecycle=LIFECYCLE_EPHEMERAL,
            namespace=participant.get("namespace"),
            run_id=participant["run_id"],
            parent_agent_id=participant["parent_agent_id"],
            crew_template=participant["crew_template"],
            crew_role=participant["role"],
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
            # Each gateway names itself so its rejects are attributable.
            source=self._source,
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.info("authentication rejected", extra=event.model_dump())
