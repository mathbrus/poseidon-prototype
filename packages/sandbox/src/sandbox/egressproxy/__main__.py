"""Egress proxy entry point:

    python -m sandbox.egressproxy

Configured by env (all set by the Helm chart):
    EGRESS_PROXY_KEY     shared HMAC key for per-run credentials (required)
    REGISTRY_DSN         Postgres DSN — audit_events plus the per-run
                         allowlist resolution (run -> registration ->
                         company blueprint)

The listen port is the platform's egress-proxy port (`common.topology`);
it is not an env var on purpose — Kubernetes service links inject
`EGRESS_PROXY_PORT=tcp://<ip>:3128` into every pod of the proxy's
namespace, which would shadow any same-named setting.
"""

import asyncio

from pydantic_settings import BaseSettings

from registry import AuditStore, Companies, SandboxRegistrations, open_registry
from sandbox.egressproxy.proxy import EgressProxy, RegistryAllowlist


class EgressProxySettings(BaseSettings):
    egress_proxy_key: str


async def main() -> None:
    settings = EgressProxySettings()
    registry = open_registry()
    proxy = EgressProxy(
        key=settings.egress_proxy_key,
        allowlist=RegistryAllowlist(SandboxRegistrations(registry), Companies(registry)),
        audit=AuditStore(registry),
    )
    server = await proxy.start()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
