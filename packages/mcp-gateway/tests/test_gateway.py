"""Gateway core tests: auth, policy pipeline, audit, sandbox proxying.

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


async def test_platform_registers_sandbox_via_its_own_decision_path(app, backends, audit):
    resp = await register_sandbox(app, RUN_ID)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    # Nothing is pushed to OPA: the registration row *is* the binding,
    # and the decision was made on the platform package.
    assert backends.decision_paths == ["/v1/data/poseidon/platform/decision"]

    events = audit_events(audit)
    assert len(events) == 1
    event = events[0]
    assert event["tool"] == "/admin/sandboxes"
    assert event["agent_id"] == "orchestrator" and event["role"] == "platform"
    assert event["decision"] == "allow" and event["backend_status"] == "ok"


async def test_agent_token_cannot_register_sandbox(app, backends, audit):
    resp = await register_sandbox(app, RUN_ID, token=AGENT_TOKEN)
    assert resp.status_code == 403
    assert resp.json()["rule"] == "default_deny"
    assert audit_events(audit)[-1]["decision"] == "deny"


async def test_register_rejects_malformed_body(app):
    async with http_client(app, RUNNER_TOKEN) as client:
        resp = await client.post("/admin/sandboxes", json={"run_id": RUN_ID})
    assert resp.status_code == 422


# --- allow path --------------------------------------------------------------


async def test_bound_agent_call_is_proxied_to_sandbox(app, backends, audit):
    await register_sandbox(app, RUN_ID)

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.read_file", {"run_id": RUN_ID, "path": "README.md"}
        )

    assert tool_payload(result)["content"] == "hello from sandbox"
    # run_id is registry routing, not a sandbox arg.
    assert backends.sandbox_calls == [("/files/read", {"path": "README.md"})]

    event = audit_events(audit)[-1]
    assert event["tool"] == "sandbox.read_file"
    assert (
        event["agent_id"] == "backend-engineer-1" and event["role"] == "backend-engineer"
    )
    assert event["run_id"] == RUN_ID
    assert event["decision"] == "allow"
    assert event["rule"] == "role_backend_engineer_sandbox.read_file"
    assert event["backend_status"] == "ok"
    assert SHA256_HEX.match(event["args_digest"])

    # The decision input, enriched: the registration made the binding.
    decision_input = backends.policy_inputs[-1]
    assert decision_input["company"] == "hello-saas"
    assert decision_input["lifecycle"] == "standing"
    assert decision_input["run_binding_verified"] is True


async def test_all_sandbox_tools_map_to_tool_server_routes(app, backends):
    await register_sandbox(app, RUN_ID)

    calls = [
        ("sandbox.read_file", {"run_id": RUN_ID, "path": "a.py"}, "/files/read"),
        (
            "sandbox.write_file",
            {"run_id": RUN_ID, "path": "a.py", "content": "x"},
            "/files/write",
        ),
        (
            "sandbox.edit_file",
            {
                "run_id": RUN_ID,
                "path": "a.py",
                "command": "str_replace",
                "old_str": "x",
                "new_str": "y",
            },
            "/files/edit",
        ),
        ("sandbox.list_dir", {"run_id": RUN_ID, "path": "."}, "/files/list"),
        ("sandbox.exec", {"run_id": RUN_ID, "cmd": "ls", "timeout_s": 5}, "/exec"),
        ("sandbox.run_tests", {"run_id": RUN_ID}, "/tests"),
        ("sandbox.git_bundle", {"run_id": RUN_ID, "branch": "feature/x"}, "/bundle"),
    ]
    async with mcp_session(app, AGENT_TOKEN) as session:
        for tool, args, _route in calls:
            result = await session.call_tool(tool, args)
            assert not result.isError, (tool, result.content)

    assert [route for route, _ in backends.sandbox_calls] == [
        route for _, _, route in calls
    ]
    exec_payload = dict(backends.sandbox_calls[4][1])
    assert exec_payload == {"cmd": "ls", "timeout_s": 5}


# --- deny path ---------------------------------------------------------------


async def test_unbound_run_is_denied_with_rule_name(app, backends, audit):
    """No live sandbox registration for the claimed run -> the enrichment
    sets run_binding_verified false -> the compiled rule cannot match, so
    a wrong or stale run_id is denied."""
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.exec", {"run_id": "run-NOTMINE", "cmd": "rm -rf /", "timeout_s": 5}
        )

    assert result.isError
    assert "default_deny" in result.content[0].text
    assert backends.sandbox_calls == []
    assert backends.policy_inputs[-1]["run_binding_verified"] is False

    event = audit_events(audit)[-1]
    assert event["decision"] == "deny" and event["rule"] == "default_deny"
    assert event["backend_status"] is None
    assert SHA256_HEX.match(event["args_digest"])


async def test_terminated_registration_is_a_stale_binding(app, backends, audit):
    """The same run allowed while its registration lives is denied once the
    registration is terminated — revocation via the registry row."""
    await register_sandbox(app, RUN_ID)
    async with mcp_session(app, AGENT_TOKEN) as session:
        live = await session.call_tool(
            "sandbox.read_file", {"run_id": RUN_ID, "path": "a.py"}
        )
        assert not live.isError

        async with http_client(app, RUNNER_TOKEN) as client:
            resp = await client.request(
                "DELETE", "/admin/sandboxes", json={"run_id": RUN_ID}
            )
            assert resp.status_code == 200

        stale = await session.call_tool(
            "sandbox.read_file", {"run_id": RUN_ID, "path": "a.py"}
        )
    assert stale.isError
    assert "default_deny" in stale.content[0].text


# --- backend failures --------------------------------------------------------


async def test_allowed_but_unregistered_run_id_errors_and_audits(app, backends, audit):
    backends.force_allow = True  # policy says yes, but no sandbox was registered

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "sandbox.read_file", {"run_id": "run-GHOST", "path": "a.py"}
        )

    assert result.isError
    assert "no sandbox registered" in result.content[0].text

    event = audit_events(audit)[-1]
    assert event["decision"] == "allow" and event["backend_status"] == "error"


async def test_sandbox_http_error_surfaces_and_audits(app, backends, audit):
    await register_sandbox(app, RUN_ID)
    backends.sandbox_error = 500

    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool("sandbox.run_tests", {"run_id": RUN_ID})

    assert result.isError
    event = audit_events(audit)[-1]
    assert event["decision"] == "allow" and event["backend_status"] == "error"


# --- audit stream ------------------------------------------------------------


async def test_every_call_appends_one_ordered_audit_line(app, backends, audit):
    await register_sandbox(app, RUN_ID)
    async with mcp_session(app, AGENT_TOKEN) as session:
        await session.call_tool("sandbox.run_tests", {"run_id": RUN_ID})
        await session.call_tool("sandbox.exec", {"run_id": "run-OTHER", "cmd": "id"})

    events = audit_events(audit)
    assert [(e["tool"], e["decision"]) for e in events] == [
        ("/admin/sandboxes", "allow"),
        ("sandbox.run_tests", "allow"),
        ("sandbox.exec", "deny"),
    ]
    assert all(SHA256_HEX.match(e["args_digest"]) for e in events)
    assert all(e["latency_ms"] >= 0 for e in events)
