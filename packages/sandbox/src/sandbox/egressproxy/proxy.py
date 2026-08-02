"""The sandbox egress proxy (plan step 5, overview §4.6 [adopted]).

An owned forward proxy — HTTP CONNECT tunneling for TLS plus absolute-form
plain HTTP — enforcing a host allowlist and emitting an audit event for
every decision, correlated to the run through the per-run credential
(`credentials.py`, the frozen §2b contract). Owned rather than off-the-shelf
because the audit contract *is* the point: NetworkPolicy drops packets
silently and stays the backstop beneath this; the proxy is what makes
"egress attempt blocked and visible in the audit store" mechanically true.

The allowlist is static config in this phase (empty = zero egress); the
compiler emits it per blueprint at plan step 12 — `_allowed` keeps the
per-run signature so that swap changes configuration, not shape.

Audit events are v1-shaped (`agent_id=egress-proxy`, `role=sandbox-infra`,
`tool=sandbox.egress`) and land in the registry `audit_events` table
alongside the gateway's, so one `run_id` query returns the whole story;
step 13 upgrades them onto the multi-source v2 contract.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from common.audit import AuditEvent
from common.telemetry import get_logger
from registry import AuditStore

from sandbox.egressproxy.credentials import verify

DEFAULT_PORT = 3128
HEAD_TIMEOUT_S = 30
CONNECT_TIMEOUT_S = 10
_HOP_HEADERS = ("proxy-authorization:", "proxy-connection:", "connection:")


def _digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class EgressProxy:
    def __init__(
        self,
        key: str,
        allow_hosts: list[str],
        audit: AuditStore,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._key = key
        self._allow = {h.strip().lower() for h in allow_hosts if h.strip()}
        self._audit_store = audit
        self._port = port
        self._log = get_logger("sandbox.egressproxy")

    async def start(self) -> asyncio.Server:
        server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)
        self._log.info(
            "egress proxy listening",
            extra={"port": self._port, "allow_hosts": sorted(self._allow)},
        )
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
            self._audit(run_id, host, port, "deny", rule, started, None)
            self._respond(
                writer,
                "407 Proxy Authentication Required",
                extra_headers='Proxy-Authenticate: Basic realm="poseidon-egress"',
            )
            return
        if not self._allowed(run_id, host):
            self._audit(run_id, host, port, "deny", "egress_not_allowlisted", started, None)
            self._respond(writer, "403 Forbidden")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            self._audit(run_id, host, port, "allow", "egress_allowlisted", started, "error")
            self._log.info("upstream connect failed", extra={"host": host, "error": str(exc)})
            self._respond(writer, "502 Bad Gateway")
            return
        self._audit(run_id, host, port, "allow", "egress_allowlisted", started, "ok")

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

    def _allowed(self, run_id: str, host: str) -> bool:
        # Static in this phase; the per-run signature is the step-12 seam.
        return host.lower() in self._allow

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

    def _audit(
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
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id="egress-proxy",
            role="sandbox-infra",
            tool="sandbox.egress",
            args_digest=_digest({"host": host, "port": port}),
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=backend_status,
        )
        self._audit_store.append(event)
        self._log.info(
            "egress",
            extra={**event.model_dump(), "host": host, "port": port},
        )
