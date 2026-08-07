"""The LLM gateway: the one route to any model.

Pipeline per completion call:

    workload authn (common.authn — same credentials as the MCP gateway)
    -> run attribution: X-Poseidon-Run-Id must name a known run of the
       caller's company (ephemeral callers additionally must be bound to
       exactly that run) — anonymous or unattributed passthrough is gone
    -> budget headroom: the applied blueprint's per-run cap against the
       ledger SUM; exhausted -> a distinguishable `budget_exhausted` error,
       audited, never silent degradation (enforcement is local here,
       no Temporal client — the workflow observes the surfaced error)
    -> model-tier routing: the caller's instance tier resolves to a model
       from cluster config; the request's own model field is overridden
    -> forward upstream (LiteLLM holding the provider keys, or the mock)
    -> meter: usage -> budget_ledger row under {company, run_id, agent,
       parent, role, model}; audit event (source=llm-gateway)

Run with: uvicorn --factory gateway_llm.app:create_app
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from common.audit import AuditEvent
from common.authn import Authenticator, Reviewer, default_reviewer
from common.identity import LIFECYCLE_EPHEMERAL, Identity
from common.telemetry import get_logger
from gateway_llm.config import LlmGatewaySettings
from registry import AuditStore, BudgetLedger, RunParticipants, Runs, open_registry
from registry.bundles import AgentInstances

TOOL = "llm.chat_completions"
RUN_HEADER = "x-poseidon-run-id"


class AuthMiddleware:
    """Same shape as the MCP gateway's: authenticate once per request,
    stash the identity, 401 otherwise (rejects audited by the
    authenticator). The health probes are the only unauthenticated routes."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in (
            "/healthz",
            "/health/liveliness",
        ):
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization")
        identity = await self.authenticator.authenticate(
            authorization, scope.get("path", "")
        )
        if identity is None:
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["identity"] = identity
        await self.app(scope, receive, send)


def _error(status: int, message: str, code: str) -> JSONResponse:
    """OpenAI-style error body: standard clients raise it with the code and
    message intact, which is what makes `budget_exhausted` distinguishable
    at the brain and therefore observable by the workflow."""
    return JSONResponse(
        {"error": {"message": message, "type": "poseidon_policy", "code": code}},
        status_code=status,
    )


def create_app(
    settings: LlmGatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    audit: AuditStore | None = None,
    ledger: BudgetLedger | None = None,
    runs: Runs | None = None,
    instances: AgentInstances | None = None,
    participants: RunParticipants | None = None,
    companies: Any | None = None,
    token_reviewer: Reviewer | None = None,
) -> FastAPI:
    settings = settings or LlmGatewaySettings()
    http_client = http_client or httpx.AsyncClient(
        base_url=settings.upstream_url, timeout=httpx.Timeout(30.0, read=600.0)
    )
    log = get_logger("gateway-llm")

    accessors = (audit, ledger, runs, instances, participants, companies)
    registry = None
    if all(a is None for a in accessors):
        from registry.bundles import Companies

        registry = open_registry()
        audit = AuditStore(registry)
        ledger = BudgetLedger(registry)
        runs = Runs(registry)
        instances = AgentInstances(registry)
        participants = RunParticipants(registry)
        companies = Companies(registry)
    elif any(a is None for a in accessors):
        raise ValueError("inject all registry accessors, or none")

    authenticator = Authenticator(
        settings,
        audit,
        instances,
        participants,
        reviewer=token_reviewer or default_reviewer(settings),
    )
    tier_models = settings.tier_model_map()

    async def emit(
        identity: Identity,
        run_id: str,
        workflow: str,
        decision: str,
        rule: str,
        digest: str,
        started: float,
        backend_status: str | None,
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=TOOL,
            args_digest=digest,
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=backend_status,
            source="llm-gateway",
            company=identity.company or "",
            lifecycle=identity.lifecycle,
            parent_agent_id=identity.parent_agent_id or "",
            workflow=workflow,
        )
        await asyncio.to_thread(audit.append, event)
        log.info("audit", extra=event.model_dump())

    app = FastAPI(title="poseidon-llm-gateway")

    @app.get("/healthz")
    @app.get("/health/liveliness")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        identity: Identity = request.state.identity
        started = time.monotonic()
        body = await request.json()
        # Digest, never content: audit must not become a leak channel.
        digest = hashlib.sha256(
            json.dumps(
                {"model": body.get("model"), "messages": len(body.get("messages", []))},
                sort_keys=True,
            ).encode()
        ).hexdigest()

        # -- run attribution (mandatory; anonymous passthrough is gone) ----
        run_id = request.headers.get(RUN_HEADER, "").strip()
        if not run_id:
            await emit(identity, "", "", "deny", "llm_unattributed", digest,
                       started, None)
            return _error(400, "missing X-Poseidon-Run-Id", "llm_unattributed")
        if identity.lifecycle == LIFECYCLE_EPHEMERAL and identity.run_id != run_id:
            await emit(identity, run_id, "", "deny", "llm_foreign_run", digest,
                       started, None)
            return _error(403, "not a participant of this run", "llm_foreign_run")
        run = await asyncio.to_thread(runs.get, run_id)
        if run is None or (
            identity.company and run.company and run.company != identity.company
        ):
            await emit(identity, run_id, "", "deny", "llm_unknown_run", digest,
                       started, None)
            return _error(403, f"unknown run {run_id!r} for this caller",
                          "llm_unknown_run")
        workflow = run.workflow or ""
        company = identity.company or run.company or ""

        # -- budget headroom (local enforcement, never silent) -------------
        def read_budget() -> tuple[float, float]:
            spec = companies.get(company) or {}
            cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
            return cap, ledger.spent_for_run(run_id)

        cap, spent = await asyncio.to_thread(read_budget)
        if cap > 0 and spent >= cap:
            await emit(identity, run_id, workflow, "deny", "budget_exhausted",
                       digest, started, None)
            return _error(
                429,
                f"budget_exhausted: run {run_id} spent {spent:.6f} USD of its"
                f" {cap:.6f} USD cap",
                "budget_exhausted",
            )

        # -- model-tier routing --------------------------------------------
        instance = await asyncio.to_thread(instances.get, identity.agent_id)
        tier = (instance or {}).get("model_tier") or ""
        model = tier_models.get(tier, settings.default_model)
        body["model"] = model

        # -- forward + meter ------------------------------------------------
        try:
            upstream = await http_client.post("/v1/chat/completions", json=body)
        except httpx.HTTPError as exc:
            await emit(identity, run_id, workflow, "allow", "budget_headroom",
                       digest, started, "error")
            return _error(502, f"upstream unreachable: {exc}", "llm_upstream_error")
        if upstream.status_code != 200:
            await emit(identity, run_id, workflow, "allow", "budget_headroom",
                       digest, started, "error")
            return JSONResponse(upstream.json(), status_code=upstream.status_code)

        payload = upstream.json()
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        usd = (prompt_tokens + completion_tokens) / 1000 * settings.price_per_1k_tokens

        await asyncio.to_thread(
            ledger.record,
            company=company,
            run_id=run_id,
            agent_id=identity.agent_id,
            parent_agent_id=identity.parent_agent_id or "",
            role=identity.role,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usd=usd,
        )
        await emit(identity, run_id, workflow, "allow", "budget_headroom",
                   digest, started, "ok")
        return JSONResponse(payload)

    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    if registry is not None:
        @app.on_event("shutdown")
        def close_registry() -> None:
            registry.close()
    return app
