"""Unit tests for the egress proxy: allow/deny/credential paths over real
sockets on localhost (plain-HTTP forward via httpx's proxy support, CONNECT
via a raw socket), with the audit store asserted per decision."""

import asyncio
import base64

import httpx
import pytest

from common.topology import COMPANY_NAMESPACE
from registry.memory import InMemoryAuditStore
from sandbox.egressproxy.credentials import (
    ProxyUnresolvable,
    mint,
    resolve_host,
    sandbox_proxy_env,
    verify,
)
from sandbox.egressproxy.proxy import EgressProxy

KEY = "unit-test-key"
RUN_ID = "run-01EGRESSTEST"


# -- credentials ----------------------------------------------------------


def test_mint_verify_roundtrip():
    secret = mint(KEY, RUN_ID)
    assert verify(KEY, RUN_ID, secret)
    assert not verify(KEY, RUN_ID, secret + "x")
    assert not verify(KEY, "run-other", secret)
    assert not verify("other-key", RUN_ID, secret)


def test_sandbox_proxy_env_carries_the_credential():
    env = sandbox_proxy_env("http://localhost:3128", KEY, RUN_ID)
    expected = f"http://{RUN_ID}:{mint(KEY, RUN_ID)}@127.0.0.1:3128"
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == expected
    assert env["http_proxy"] == env["https_proxy"] == expected
    assert "localhost" in env["NO_PROXY"]


def test_proxy_address_is_resolved_for_the_sandbox():
    """The sandbox has no DNS, so the one name it would have needed — the
    proxy's own — is resolved here, at provision time."""
    assert resolve_host("http://localhost:3128") == "http://127.0.0.1:3128"
    # A URL without an explicit port keeps not having one.
    assert resolve_host("http://localhost") == "http://127.0.0.1"


def test_unresolvable_proxy_host_fails_at_provision():
    """Passing a hostname through would guarantee a DNS failure inside the
    sandbox, which has no resolver — so this fails where there is one."""
    with pytest.raises(ProxyUnresolvable):
        resolve_host("http://nothing.invalid:3128")
    with pytest.raises(ProxyUnresolvable):
        resolve_host("egress-proxy:3128")  # no scheme, so no host either


# -- proxy behaviour ------------------------------------------------------

UPSTREAM_BODY = b"upstream-ok"


async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    await reader.readuntil(b"\r\n\r\n")
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
        % (len(UPSTREAM_BODY), UPSTREAM_BODY)
    )
    await writer.drain()
    writer.close()


class Stack:
    def __init__(self, proxy_port: int, upstream_port: int, audit: InMemoryAuditStore):
        self.proxy_port = proxy_port
        self.upstream_port = upstream_port
        self.audit = audit

    @property
    def proxy_url(self) -> str:
        return f"http://{RUN_ID}:{mint(KEY, RUN_ID)}@127.0.0.1:{self.proxy_port}"

    def events(self) -> list[dict]:
        return [event.model_dump() for event in self.audit.events]


async def _with_stack(scenario):
    audit = InMemoryAuditStore()
    upstream = await asyncio.start_server(_upstream, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    # A per-run resolver: RUN_ID's company allows the local
    # upstream; any other run resolves to zero egress.
    class OneRunAllowlist:
        def hosts_for(self, run_id: str) -> set[str]:
            return {"127.0.0.1"} if run_id == RUN_ID else set()

    proxy = EgressProxy(key=KEY, allowlist=OneRunAllowlist(), audit=audit, port=0)
    server = await proxy.start()
    proxy_port = server.sockets[0].getsockname()[1]
    try:
        await scenario(Stack(proxy_port, upstream_port, audit))
    finally:
        server.close()
        upstream.close()
        await server.wait_closed()
        await upstream.wait_closed()


def test_allowlisted_http_request_is_forwarded_and_audited():
    async def scenario(stack: Stack):
        async with httpx.AsyncClient(proxy=stack.proxy_url) as client:
            resp = await client.get(f"http://127.0.0.1:{stack.upstream_port}/x")
        assert resp.status_code == 200
        assert resp.content == UPSTREAM_BODY
        (event,) = stack.events()
        assert event["decision"] == "allow"
        assert event["rule"] == "egress_allowlisted"
        assert event["run_id"] == RUN_ID
        assert event["backend_status"] == "ok"

    asyncio.run(_with_stack(scenario))


def test_non_allowlisted_host_is_denied_and_audited():
    async def scenario(stack: Stack):
        async with httpx.AsyncClient(proxy=stack.proxy_url) as client:
            # Plain-HTTP denials come back as the proxy's own response.
            resp = await client.get("http://denied.example.com/")
        assert resp.status_code == 403
        (event,) = stack.events()
        assert event["decision"] == "deny"
        assert event["rule"] == "egress_not_allowlisted"
        assert event["run_id"] == RUN_ID

    asyncio.run(_with_stack(scenario))


def test_bad_credential_is_denied_with_claimed_run_id():
    async def scenario(stack: Stack):
        bad = f"http://{RUN_ID}:not-the-secret@127.0.0.1:{stack.proxy_port}"
        async with httpx.AsyncClient(proxy=bad) as client:
            resp = await client.get(f"http://127.0.0.1:{stack.upstream_port}/")
        assert resp.status_code == 407
        (event,) = stack.events()
        assert event["decision"] == "deny"
        assert event["rule"] == "egress_bad_credential"
        assert event["run_id"] == RUN_ID  # the unverified claim stays visible

    asyncio.run(_with_stack(scenario))


def test_missing_credential_is_denied():
    async def scenario(stack: Stack):
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{stack.proxy_port}"
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{stack.upstream_port}/")
        assert resp.status_code == 407
        (event,) = stack.events()
        assert event["rule"] == "egress_no_credential"

    asyncio.run(_with_stack(scenario))


def test_connect_tunnel_to_allowlisted_host():
    async def scenario(stack: Stack):
        reader, writer = await asyncio.open_connection("127.0.0.1", stack.proxy_port)
        auth = base64.b64encode(f"{RUN_ID}:{mint(KEY, RUN_ID)}".encode()).decode()
        writer.write(
            (
                f"CONNECT 127.0.0.1:{stack.upstream_port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{stack.upstream_port}\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        established = await reader.readuntil(b"\r\n\r\n")
        assert b"200 Connection Established" in established
        writer.write(b"GET / HTTP/1.1\r\nHost: upstream\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert UPSTREAM_BODY in response
        writer.close()
        (event,) = stack.events()
        assert event["decision"] == "allow"
        assert event["backend_status"] == "ok"

    asyncio.run(_with_stack(scenario))


def test_healthz_answers_without_credentials():
    async def scenario(stack: Stack):
        reader, writer = await asyncio.open_connection("127.0.0.1", stack.proxy_port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: proxy\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert b"200 OK" in response
        writer.close()
        assert stack.events() == []  # probes are not audit noise

    asyncio.run(_with_stack(scenario))


def test_registry_allowlist_resolves_run_to_company_projection():
    """run -> live registration -> company by namespace -> the blueprint's
    egress_proxy_allowlist; no registration or no blueprint means zero
    egress, and termination revokes within the cache TTL."""
    from registry.memory import InMemoryCompanies, InMemorySandboxRegistrations
    from sandbox.egressproxy.proxy import RegistryAllowlist

    sandboxes = InMemorySandboxRegistrations()
    companies = InMemoryCompanies()
    companies.upsert(
        "hello-saas",
        {
            "namespace": COMPANY_NAMESPACE,
            "infra": {"egress_proxy_allowlist": ["PyPI.org", " files.pythonhosted.org "]},
        },
    )
    allowlist = RegistryAllowlist(sandboxes, companies, ttl_s=0)

    assert allowlist.hosts_for(RUN_ID) == set()  # no registration yet

    sandboxes.register(RUN_ID, "http://10.0.0.9:8000", COMPANY_NAMESPACE, "sbx")
    assert allowlist.hosts_for(RUN_ID) == {"pypi.org", "files.pythonhosted.org"}

    sandboxes.terminate(RUN_ID)
    assert allowlist.hosts_for(RUN_ID) == set()

    sandboxes.register("run-other", "http://10.0.0.10:8000", "nowhere-ns", "sbx2")
    assert allowlist.hosts_for("run-other") == set()  # no applied blueprint
