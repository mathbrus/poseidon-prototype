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
import os

from registry import AuditStore, SandboxRegistrations, open_registry
from registry.bundles import Companies
from sandbox.egressproxy.proxy import DEFAULT_PORT, EgressProxy, RegistryAllowlist


async def main() -> None:
    registry = open_registry()
    proxy = EgressProxy(
        key=os.environ["EGRESS_PROXY_KEY"],
        allowlist=RegistryAllowlist(
            SandboxRegistrations(registry), Companies(registry)
        ),
        audit=AuditStore(registry),
        port=int(os.environ.get("EGRESS_LISTEN_PORT", DEFAULT_PORT)),
    )
    server = await proxy.start()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
