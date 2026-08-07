"""The sandbox egress proxy.

An owned forward proxy — HTTP CONNECT tunneling for TLS plus absolute-form
plain HTTP — enforcing a host allowlist and emitting an audit event for
every decision, correlated to the run through the per-run credential
(`credentials.py`). Owned rather than off-the-shelf
because the audit contract *is* the point: NetworkPolicy drops packets
silently and stays the backstop beneath this; the proxy is what makes
"egress attempt blocked and visible in the audit store" mechanically true.

The allowlist is the blueprint's compiled IR projection:
each request's run resolves through its live
sandbox registration to the company whose applied blueprint names the
allowed hosts. No applied blueprint, no live registration — zero egress.

Audit events ride the shared ingestion contract:
`source=sandbox-infra`, `tool=sandbox.egress`, `agent_id=egress-proxy` —
they land in the registry `audit_events` table alongside the gateway's, so
one `run_id` query returns the whole story.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from common.audit import AuditEvent
from common.telemetry import get_logger
from registry import AuditStore
from sandbox.egressproxy.credentials import verify

DEFAULT_PORT = 3128
HEAD_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10
_HOP_HEADERS = ("proxy-authorization:", "proxy-connection:", "connection:")


class Allowlist(Protocol):
    def hosts_for(self, run_id: str) -> set[str]:
        """The hosts this run may reach. Synchronous (registry reads);
        callers run it off the event loop."""
        ...


class RegistryAllowlist:
    """run_id -> live sandbox registration -> company (by namespace) ->
    the blueprint's `egress_proxy_allowlist` projection. Read-only registry
    access; briefly cached so a chatty `pip install` costs two queries, not
    two per connection."""

    def __init__(self, sandboxes, companies, ttl_s: float = 10.0) -> None:
        self._sandboxes = sandboxes
        self._companies = companies
        self._ttl = ttl_s
        self._cache: dict[str, tuple[set[str], float]] = {}

    def hosts_for(self, run_id: str) -> set[str]:
        cached = self._cache.get(run_id)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        hosts: set[str] = set()
        registration = self._sandboxes.active(run_id)
        if registration is not None and registration.get("namespace"):
            spec = self._companies.by_namespace(registration["namespace"]) or {}
            hosts = {
                h.strip().lower()
                for h in (spec.get("infra") or {}).get("egress_proxy_allowlist") or []
                if h.strip()
            }
        self._cache = {
            k: v for k, v in self._cache.items() if v[1] > time.monotonic()
        }
        self._cache[run_id] = (hosts, time.monotonic() + self._ttl)
        return hosts


def _digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class EgressProxy:
    def __init__(
        self,
        key: str,
        allowlist: Allowlist,
        audit: AuditStore,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._key = key
        self._allowlist = allowlist
        self._audit_store = audit
        self._port = port
        self._log = get_logger("sandbox.egressproxy")

    async def start(self) -> asyncio.Server:
        server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)
        self._log.info("egress proxy listening", extra={"port": self._port})
        return server

    # -- request handling --------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._serve(reader, writer)
        except Exception as exc:  # a broken client must never kill the server
            self._log.info("connection error", extra={"error": str(exc)})
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        started = time.monotonic()
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_S
            )
        except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError):
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            self._respond(writer, "400 Bad Request")
            return
        headers = {
            key.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for key, _, value in [line.partition(":")]
        }

        if target.startswith("/"):  # origin-form: only the kubelet probe
            status = "200 OK" if method == "GET" and target == "/healthz" else "400 Bad Request"
            self._respond(writer, status, b"ok" if status.startswith("200") else b"")
            return

        if method == "CONNECT":
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 443
        else:
            url = urlsplit(target)
            host = url.hostname or ""
            port = url.port or 80

        run_id, rule = self._authenticate(headers)
        if rule is not None:
            await self._audit(run_id, host, port, "deny", rule, started, None)
            self._respond(
                writer,
                "407 Proxy Authentication Required",
                extra_headers='Proxy-Authenticate: Basic realm="poseidon-egress"',
            )
            return
        if not await self._allowed(run_id, host):
            await self._audit(run_id, host, port, "deny", "egress_not_allowlisted", started, None)
            self._respond(writer, "403 Forbidden")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            await self._audit(run_id, host, port, "allow", "egress_allowlisted", started, "error")
            self._log.info("upstream connect failed", extra={"host": host, "error": str(exc)})
            self._respond(writer, "502 Bad Gateway")
            return
        await self._audit(run_id, host, port, "allow", "egress_allowlisted", started, "ok")

        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(self._rewrite(method, target, lines[1:]))
                await upstream_writer.drain()
            await self._tunnel(reader, writer, upstream_reader, upstream_writer)
        finally:
            with contextlib.suppress(Exception):
                upstream_writer.close()
                await upstream_writer.wait_closed()

    def _authenticate(self, headers: dict[str, str]) -> tuple[str, str | None]:
        """Returns (run_id, deny_rule); deny_rule None means verified. The
        claimed run_id is recorded even when verification fails, so failed
        attribution attempts stay visible."""
        value = headers.get("proxy-authorization", "")
        if not value.lower().startswith("basic "):
            return "", "egress_no_credential"
        try:
            decoded = base64.b64decode(value[6:].strip()).decode()
        except Exception:
            return "", "egress_bad_credential"
        run_id, _, secret = decoded.partition(":")
        if not verify(self._key, run_id, secret):
            return run_id, "egress_bad_credential"
        return run_id, None

    async def _allowed(self, run_id: str, host: str) -> bool:
        # Registry reads are synchronous psycopg; keep them off the loop.
        hosts = await asyncio.to_thread(self._allowlist.hosts_for, run_id)
        return host.lower() in hosts

    @staticmethod
    def _rewrite(method: str, target: str, header_lines: list[str]) -> bytes:
        """Absolute-form request -> origin-form for the upstream, hop-by-hop
        headers stripped, keep-alive disabled (one request per connection
        keeps the pipe logic honest; TLS traffic never comes through here)."""
        url = urlsplit(target)
        origin = url.path or "/"
        if url.query:
            origin += f"?{url.query}"
        kept = [
            line
            for line in header_lines
            if line and not line.lower().startswith(_HOP_HEADERS)
        ]
        head = "\r\n".join([f"{method} {origin} HTTP/1.1", *kept, "Connection: close"])
        return head.encode("latin-1") + b"\r\n\r\n"

    @staticmethod
    async def _tunnel(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(1 << 16):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                # Half-close so request/response framing that relies on EOF
                # still completes while the other direction drains.
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        await asyncio.gather(
            pump(client_reader, upstream_writer),
            pump(upstream_reader, client_writer),
        )

    # -- responses and audit ----------------------------------------------

    @staticmethod
    def _respond(
        writer: asyncio.StreamWriter,
        status: str,
        body: bytes = b"",
        extra_headers: str = "",
    ) -> None:
        extra = f"{extra_headers}\r\n" if extra_headers else ""
        writer.write(
            (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"{extra}"
                "Connection: close\r\n\r\n"
            ).encode()
            + body
        )

    async def _audit(
        self,
        run_id: str,
        host: str,
        port: int,
        decision: str,
        rule: str,
        started: float,
        backend_status: str | None,
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            agent_id="egress-proxy",
            # The enforcement point is the `source` field —
            # role is a caller property this producer does not have.
            role="",
            tool="sandbox.egress",
            args_digest=_digest({"host": host, "port": port}),
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=backend_status,
            source="sandbox-infra",
        )
        # The store is synchronous psycopg; keep it off the event loop or a
        # slow Postgres stalls every tunnel and the /healthz probe with it.
        await asyncio.to_thread(self._audit_store.append, event)
        self._log.info(
            "egress",
            extra={**event.model_dump(), "host": host, "port": port},
        )
