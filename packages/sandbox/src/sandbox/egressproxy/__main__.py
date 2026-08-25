"""Egress proxy entry point:

    python -m sandbox.egressproxy

Configured by env (all set by the Helm chart):
    EGRESS_PROXY_KEY     shared HMAC key for per-run credentials (required)
    REGISTRY_DSN         Postgres DSN — audit_events plus the per-run
                         allowlist resolution (run -> registration ->
                         company blueprint)
    EGRESS_LISTEN_PORT   listen port (default 3128) — deliberately NOT
                         EGRESS_PROXY_PORT, which Kubernetes service links
                         shadow with `tcp://<ip>:3128` for every pod in the
                         proxy's namespace
"""

import asyncio

from pydantic_settings import BaseSettings

from registry import AuditStore, Companies, SandboxRegistrations, open_registry
from sandbox.egressproxy.proxy import DEFAULT_PORT, EgressProxy, RegistryAllowlist


class EgressProxySettings(BaseSettings):
    egress_proxy_key: str
    egress_listen_port: int = DEFAULT_PORT


async def main() -> None:
    settings = EgressProxySettings()
    registry = open_registry()
    proxy = EgressProxy(
        key=settings.egress_proxy_key,
        allowlist=RegistryAllowlist(SandboxRegistrations(registry), Companies(registry)),
        audit=AuditStore(registry),
        port=settings.egress_listen_port,
    )
    server = await proxy.start()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
