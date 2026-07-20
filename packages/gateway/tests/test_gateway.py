"""S5 gateway core tests: auth, policy pipeline, audit, sandbox proxying.

OPA and the sandbox tool server are stubbed behind an httpx.MockTransport
(see conftest); the MCP surface is exercised through a real MCP client over
an in-process ASGI transport.
"""

import json
import re

import pytest

from conftest import (
    AGENT_TOKEN,
    RUNNER_TOKEN,
    audit_events,
    http_client,
    mcp_session,
    register_sandbox,
)

pytestmark = pytest.mark.anyio

RUN_ID = "run-01TESTULID"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def tool_payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


# --- auth -------------------------------------------------------------------


async def test_unknown_token_is_rejected_with_401(app):
    async with http_client(app, "wrong-token") as client:
        mcp_resp = await client.post("/mcp", json={})
        admin_resp = await client.post(
            "/admin/sandboxes", json={"run_id": RUN_ID, "url": "http://sbx.test:8000"}
        )
    assert mcp_resp.status_code == 401
    assert admin_resp.status_code == 401


async def test_missing_token_is_rejected_with_401(app):
    async with http_client(app, None) as client:
        resp = await client.post("/mcp", json={})
    assert resp.status_code == 401


# --- admin registration -----------------------------------------------------


async def test_runner_registers_sandbox_and_assignment_reaches_opa(app, backends, audit_path):
    resp = await register_sandbox(app, RUN_ID)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert backends.assignments == {"backend-engineer-1": [RUN_ID]}

    events = audit_events(audit_path)
    assert len(events) == 1
    event = events[0]
    assert event["tool"] == "/admin/sandboxes"
    assert event["agent_id"] == "runner" and event["role"] == "runner"
    assert event["decision"] == "allow" and event["backend_status"] == "ok"


async def test_agent_token_cannot_register_sandbox(app, backends, audit_path):
    resp = await register_sandbox(app, RUN_ID, token=AGENT_TOKEN)
    assert resp.status_code == 403
    assert resp.json()["rule"] == "default_deny"
    assert backends.assignments == {}
    assert audit_events(audit_path)[-1]["decision"] == "deny"


async def test_register_rejects_malformed_body(app):
    async with http_client(app, RUNNER_TOKEN) as client:
        resp = await client.post("/admin/sandboxes", json={"run_id": RUN_ID})
    assert resp.status_code == 422


# --- allow path --------------------------------------------------------------


async def test_assigned_agent_call_is_proxied_to_sandbox(app, backends, audit_path):
    await register_sandbox(app, RUN_ID)

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.read_file", {"run_id": RUN_ID, "path": "README.md"}
        )

    assert tool_payload(result)["content"] == "hello from sandbox"
    # run_id is registry routing, not a sandbox arg (contract C4).
    assert backends.sandbox_calls == [("/files/read", {"path": "README.md"})]

    event = audit_events(audit_path)[-1]
    assert event["tool"] == "sandbox.read_file"
    assert event["agent_id"] == "backend-engineer-1" and event["role"] == "backend-engineer"
    assert event["run_id"] == RUN_ID
    assert event["decision"] == "allow" and event["rule"] == "sandbox_assigned_run"
    assert event["backend_status"] == "ok"
    assert SHA256_HEX.match(event["args_digest"])


async def test_all_six_sandbox_tools_map_to_c4_routes(app, backends):
    await register_sandbox(app, RUN_ID)

    calls = [
        ("sandbox.read_file", {"run_id": RUN_ID, "path": "a.py"}, "/files/read"),
        ("sandbox.write_file", {"run_id": RUN_ID, "path": "a.py", "content": "x"}, "/files/write"),
        ("sandbox.list_dir", {"run_id": RUN_ID, "path": "."}, "/files/list"),
        ("sandbox.exec", {"run_id": RUN_ID, "cmd": "ls", "timeout_s": 5}, "/exec"),
        ("sandbox.run_tests", {"run_id": RUN_ID}, "/tests"),
        ("sandbox.git_bundle", {"run_id": RUN_ID, "branch": "feature/x"}, "/bundle"),
    ]
    async with mcp_session(app, AGENT_TOKEN) as session:
        for tool, args, _route in calls:
            result = await session.call_tool(tool, args)
            assert not result.isError, (tool, result.content)

    assert [route for route, _ in backends.sandbox_calls] == [route for _, _, route in calls]
    exec_payload = dict(backends.sandbox_calls[3][1])
    assert exec_payload == {"cmd": "ls", "timeout_s": 5}


# --- deny path ---------------------------------------------------------------


async def test_unassigned_run_is_denied_with_rule_name(app, backends, audit_path):
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.exec", {"run_id": "run-NOTMINE", "cmd": "rm -rf /", "timeout_s": 5}
        )

    assert result.isError
    assert "default_deny" in result.content[0].text
    assert backends.sandbox_calls == []

    event = audit_events(audit_path)[-1]
    assert event["decision"] == "deny" and event["rule"] == "default_deny"
    assert event["backend_status"] is None
    assert SHA256_HEX.match(event["args_digest"])


# --- backend failures --------------------------------------------------------


async def test_allowed_but_unregistered_run_id_errors_and_audits(app, backends, audit_path):
    backends.force_allow = True  # policy says yes, but no sandbox was registered

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.read_file", {"run_id": "run-GHOST", "path": "a.py"}
        )

    assert result.isError
    assert "no sandbox registered" in result.content[0].text

    event = audit_events(audit_path)[-1]
    assert event["decision"] == "allow" and event["backend_status"] == "error"


async def test_sandbox_http_error_surfaces_and_audits(app, backends, audit_path):
    await register_sandbox(app, RUN_ID)
    backends.sandbox_error = 500

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.run_tests", {"run_id": RUN_ID}
        )

    assert result.isError
    event = audit_events(audit_path)[-1]
    assert event["decision"] == "allow" and event["backend_status"] == "error"


# --- audit stream ------------------------------------------------------------


async def test_every_call_appends_one_ordered_audit_line(app, backends, audit_path):
    await register_sandbox(app, RUN_ID)
    async with mcp_session(app, AGENT_TOKEN) as session:
        await session.call_tool("sandbox.run_tests", {"run_id": RUN_ID})
        await session.call_tool("sandbox.exec", {"run_id": "run-OTHER", "cmd": "id"})

    events = audit_events(audit_path)
    assert [(e["tool"], e["decision"]) for e in events] == [
        ("/admin/sandboxes", "allow"),
        ("sandbox.run_tests", "allow"),
        ("sandbox.exec", "deny"),
    ]
    assert all(SHA256_HEX.match(e["args_digest"]) for e in events)
    assert all(e["latency_ms"] >= 0 for e in events)
