"""The bootstrap filing: the ticket the platform files for a company
document absent on the base ref, which the stewardship crew fulfils.

Retry safety: `heartbeat_bootstrap` files under the platform identity and
is not idempotent, so the workflow never retries it (`SINGLE_ATTEMPT`) —
a filing that fails leaves the next tick to file for what is still
absent. The ticket filed is on the registry the moment the gateway
created it, so the next tick reads it before the host lists it.
"""

import asyncio
import secrets
from typing import Any

from temporalio import activity

from common.audit import args_digest
from common.telemetry import get_logger
from common.workflows import heartbeat_run_id
from orchestrator.activities.blueprint import company_repo
from orchestrator.activities.company import applied_company
from orchestrator.activities.documents import (
    BOOTSTRAP_TITLES,
    bootstrap_body,
    company_documents,
    read_documents,
)
from orchestrator.activities.heartbeat import emit_heartbeat_event
from orchestrator.clients import GatewayClient
from orchestrator.config import WorkerSettings
from registry import AuditStore, Companies


class BootstrapActivities:
    def __init__(
        self, settings: WorkerSettings, companies: Companies, audit: AuditStore
    ) -> None:
        self._settings = settings
        self._companies = companies
        self._audit = audit
        self._log = get_logger("orchestrator.heartbeat")

    async def _repo(self) -> dict[str, str]:
        return await asyncio.to_thread(company_repo, self._companies, self._company())

    def _company(self) -> str:
        return applied_company(self._companies)

    @activity.defn(name="heartbeat_bootstrap")
    async def heartbeat_bootstrap(self, names: list[str]) -> list[str]:
        """File the bootstrap ticket for each named absent document under
        the platform identity — the stewardship crew fulfils it on a later
        tick like any other ticket — and journal the filing. Returns the
        names filed.

        Absence is confirmed first: the git host's reads are eventually
        consistent, and a document that answers present now (a delivery
        merged since, a lagging read) is not filed for. The ticket filed
        is on the registry the moment the gateway created it, so the next
        tick — this one's own wake — reads it before the host lists it."""
        paths = await asyncio.to_thread(
            company_documents, self._companies, self._company()
        )
        repo = await self._repo()
        filed: list[str] = []
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            documents = await read_documents(
                gateway, heartbeat_run_id(self._company()), repo, paths
            )
            for name in names:
                if documents[name]["present"]:
                    self._log.info(
                        "document present on re-read; nothing filed",
                        extra={"document": name},
                    )
                    continue
                branch = f"feature/{name}-{secrets.token_hex(3)}"
                await gateway.create_issue(
                    heartbeat_run_id(self._company()),
                    repo["url"],
                    BOOTSTRAP_TITLES[name],
                    bootstrap_body(name, paths[name], branch),
                    f"the blueprint names {paths[name]} and it is absent on the "
                    "base ref; the company cannot scope work without it",
                )
                filed.append(name)
                await emit_heartbeat_event(
                    self._audit,
                    self._company(),
                    run_id=heartbeat_run_id(self._company()),
                    tool="heartbeat.bootstrap",
                    digest=args_digest({"document": name, "path": paths[name]}),
                    decision="allow",
                    rule=f"bootstrap_filed:{name}",
                )
                self._log.info("bootstrap ticket filed", extra={"document": name})
        return filed

    def all(self) -> list[Any]:
        return [self.heartbeat_bootstrap]
