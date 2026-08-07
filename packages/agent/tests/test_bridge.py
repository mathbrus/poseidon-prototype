"""GatewayBridge against a REAL MCP streamable-http server.

The unit doubles elsewhere replace the bridge, so this is the one test that
exercises the actual session plumbing — which is where the first cluster
run of the brain host died: the MCP client's anyio cancel scopes are
task-bound, and a bridge that enters and exits the session from separately
scheduled coroutines corrupts them on teardown. Kept as a real-server test
so session lifecycle bugs fail here, not on kind."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from agent.brain import GatewayBridge
from agent.gateway_client import ToolCallError


@pytest.fixture(scope="module")
def mcp_server_url():
    server_mcp = FastMCP("test-gateway", stateless_http=True)

    @server_mcp.tool(name="sandbox.echo")
    def echo(run_id: str, value: str) -> dict:
        return {"echoed": value, "run_id": run_id}

    @server_mcp.tool(name="sandbox.deny")
    def deny(run_id: str) -> dict:
        raise ValueError("denied by policy rule: default_deny (sandbox.deny)")

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(
        uvicorn.Config(
            server_mcp.streamable_http_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while True:
        try:
            httpx.get(base, timeout=1)
            break
        except httpx.HTTPError:
            assert time.monotonic() < deadline, "MCP server never came up"
            time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def test_bridge_session_lifecycle_and_calls(mcp_server_url):
    """Enter, call repeatedly (each call a separate coroutine, as the SDK
    executors do), and exit cleanly — the teardown is the part that breaks
    if session enter/exit ever split across tasks again."""
    with GatewayBridge(mcp_server_url, "test-token") as bridge:
        first = bridge.call("sandbox.echo", {"run_id": "r-1", "value": "one"})
        second = bridge.call("sandbox.echo", {"run_id": "r-1", "value": "two"})
    assert first == {"echoed": "one", "run_id": "r-1"}
    assert second == {"echoed": "two", "run_id": "r-1"}


def test_bridge_surfaces_tool_errors_as_toolcallerror(mcp_server_url):
    with GatewayBridge(mcp_server_url, "test-token") as bridge:
        with pytest.raises(ToolCallError) as excinfo:
            bridge.call("sandbox.deny", {"run_id": "r-1"})
        assert "default_deny" in str(excinfo.value)
        # The session survives a tool error; the bridge still works.
        after = bridge.call("sandbox.echo", {"run_id": "r-1", "value": "ok"})
        assert after["echoed"] == "ok"


def test_bridge_reenters_cleanly(mcp_server_url):
    """Two sequential dispatch lifetimes on one process
    (plan → implement → deliver each open their own bridge)."""
    for round_ in ("a", "b"):
        with GatewayBridge(mcp_server_url, "test-token") as bridge:
            result = bridge.call("sandbox.echo", {"run_id": "r-2", "value": round_})
            assert result["echoed"] == round_
