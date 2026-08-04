"""Workload identity verification (plan step 11, overview §4.9).

One `Authenticator` resolves a bearer credential to an `Identity`:

  platform JWT      -> §2b ephemeral contract: HS256 signature + audience +
                       expiry AND a live `run_participants` row, so a
                       registry row termination revokes before TTL expiry
  anything else     -> Kubernetes TokenReview with our audience; the
                       ServiceAccount maps to an identity:
                         platform ns / orchestrator SA -> platform lifecycle
                         <company ns> / <agent_id>     -> the registry's
                       `agent_instances` row, whose recorded namespace must
                       match the token's — the same SA name anywhere else
                       is an impersonation attempt and is refused

Every rejection of a *presented* token is audited (`authn_*` rules): a
token from the wrong service account or namespace must be visible in the
audit store, not just 401'd (the step-11 done-when).
"""

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from common import platformjwt
from common.audit import AuditEvent
from common.telemetry import get_logger
from gateway.config import GatewaySettings
from gateway.identity import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PLATFORM,
    LIFECYCLE_STANDING,
    ORCHESTRATOR_ID,
    PLATFORM_ROLE,
    Identity,
    bearer_token,
)

TOKENREVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"
_EMPTY_DIGEST = hashlib.sha256(b"{}").hexdigest()


@dataclass(frozen=True)
class ServiceAccount:
    namespace: str
    name: str

    @property
    def username(self) -> str:
        return f"system:serviceaccount:{self.namespace}:{self.name}"


class Reviewer(Protocol):
    async def review(self, token: str, audience: str) -> ServiceAccount | None: ...


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
        self._log = get_logger("gateway.authn")

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
            self._log.info("token review: non-serviceaccount principal",
                           extra={"username": username})
            return None
        namespace, _, name = rest.partition(":")
        if not namespace or not name:
            return None
        return ServiceAccount(namespace=namespace, name=name)


def default_reviewer(settings: GatewaySettings) -> KubeTokenReviewer | None:
    """The in-cluster reviewer, or None outside a cluster (unit tests)."""
    api_url = settings.kube_api_url
    if not api_url:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if host:
            api_url = f"https://{host}:{port}"
    if not api_url or not os.path.exists(settings.kube_token_file):
        return None
    return KubeTokenReviewer(
        api_url, settings.kube_token_file, settings.kube_ca_file
    )


class Authenticator:
    """Bearer header in, Identity out; rejects audited on the way."""

    def __init__(
        self,
        settings: GatewaySettings,
        audit: Any,
        instances: Any,
        participants: Any,
        reviewer: Reviewer | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._instances = instances
        self._participants = participants
        self._reviewer = reviewer
        self._log = get_logger("gateway.authn")
        # token digest -> (identity, monotonic deadline); TokenReview
        # successes only. Bounded: expired entries are swept on insert.
        self._cache: dict[str, tuple[Identity, float]] = {}

    async def authenticate(self, authorization: str | None, path: str) -> Identity | None:
        token = bearer_token(authorization)
        if token is None:
            # No credential presented (probes, scanners) — a 401 with no
            # audit event; only rejected *tokens* are governance signals.
            return None

        if platformjwt.issuer_of(token) == platformjwt.ISSUER:
            return await self._verify_platform_jwt(token, path)
        return await self._verify_kube_token(token, path)

    # -- platform-signed ephemeral JWTs (§2b) ------------------------------

    async def _verify_platform_jwt(self, token: str, path: str) -> Identity | None:
        key = self._settings.platform_jwt_key
        if not key:
            await self._reject(path, "authn_platform_jwt_disabled", "unknown")
            return None
        try:
            claims = platformjwt.verify(key, token, self._settings.token_audience)
        except platformjwt.TokenError as err:
            await self._reject(path, f"authn_{err.reason}", "unknown")
            return None
        agent_id = str(claims.get("sub", ""))
        # Signature alone is never enough: the live participants row is what
        # makes revocation a registry write, effective before TTL expiry.
        row = await asyncio.to_thread(self._participants.active, agent_id)
        if row is None or row["run_id"] != claims.get("run_id"):
            await self._reject(path, "authn_no_live_participant", agent_id or "unknown")
            return None
        # §5.1: `role` carries the catalog template (the minted claim);
        # the row carries the crew coordinates and parentage — the pair the
        # compiled crew rules key on (crew_template + crew_role).
        return Identity(
            agent_id=agent_id,
            role=str(claims.get("role", "")),
            company=claims.get("company"),
            lifecycle=LIFECYCLE_EPHEMERAL,
            run_id=row["run_id"],
            parent_agent_id=row["parent_agent_id"],
            crew_template=row["crew_template"],
            crew_role=row["role"],
        )

    # -- projected service-account tokens ----------------------------------

    async def _verify_kube_token(self, token: str, path: str) -> Identity | None:
        if self._reviewer is None:
            await self._reject(path, "authn_unknown_token", "unknown")
            return None

        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        try:
            account = await self._reviewer.review(token, self._settings.token_audience)
        except Exception as exc:
            self._log.info("token review failed", extra={"error": str(exc)})
            await self._reject(path, "authn_review_unavailable", "unknown")
            return None
        if account is None:
            await self._reject(path, "authn_token_rejected", "unknown")
            return None

        identity = await self._map_service_account(account, path)
        if identity is not None:
            self._cache = {
                k: v for k, v in self._cache.items() if v[1] > time.monotonic()
            }
            self._cache[digest] = (
                identity,
                time.monotonic() + self._settings.authn_cache_ttl_s,
            )
        return identity

    async def _map_service_account(
        self, account: ServiceAccount, path: str
    ) -> Identity | None:
        settings = self._settings
        if (
            account.namespace == settings.platform_namespace
            and account.name == settings.orchestrator_service_account
        ):
            # The orchestrator's privileged identity (§4.9: platform
            # lifecycle); policy keys on the lifecycle via the platform
            # decision path.
            return Identity(
                agent_id=ORCHESTRATOR_ID,
                role=PLATFORM_ROLE,
                lifecycle=LIFECYCLE_PLATFORM,
            )
        row = await asyncio.to_thread(self._instances.get, account.name)
        if row is None:
            await self._reject(path, "authn_unknown_service_account", account.username)
            return None
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

    # -- audited rejection --------------------------------------------------

    async def _reject(self, path: str, rule: str, principal: str) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id="",
            agent_id=principal,
            role="",
            tool=path,
            args_digest=_EMPTY_DIGEST,
            decision="deny",
            rule=rule,
            latency_ms=0,
            backend_status=None,
            # v2 (§5.2): an authn reject is the gateway speaking about a
            # credential that mapped to nothing — no company, no lifecycle.
            source="gateway",
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.info("authentication rejected", extra=event.model_dump())
