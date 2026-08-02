"""Egress proxy entry point:

    python -m sandbox.egressproxy

Configured by env (all set by the Helm chart):
    EGRESS_PROXY_KEY     shared HMAC key for per-run credentials (required)
    EGRESS_ALLOW_HOSTS   comma-separated host allowlist; empty = zero egress
    REGISTRY_DSN         Postgres DSN for the audit_events table
    EGRESS_LISTEN_PORT   listen port (default 3128) — deliberately NOT
                         EGRESS_PROXY_PORT, which Kubernetes service links
                         shadow with `tcp://<ip>:3128` for every pod in the
                         proxy's namespace
"""

import asyncio
import os

from registry import AuditStore, open_registry
from sandbox.egressproxy.proxy import DEFAULT_PORT, EgressProxy


async def main() -> None:
    registry = open_registry()
    proxy = EgressProxy(
        key=os.environ["EGRESS_PROXY_KEY"],
        allow_hosts=os.environ.get("EGRESS_ALLOW_HOSTS", "").split(","),
        audit=AuditStore(registry),
        port=int(os.environ.get("EGRESS_LISTEN_PORT", DEFAULT_PORT)),
    )
    server = await proxy.start()
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
