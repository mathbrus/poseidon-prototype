"""Gateway core tests: auth, policy pipeline, audit, sandbox proxying.

OPA and the sandbox tool server are stubbed behind an httpx.MockTransport
(see conftest); the MCP surface is exercised through a real MCP client over
an in-process ASGI transport.
"""

import json
import re

import pytest
from conftest import (
    AGENT_ID,
    AGENT_TOKEN,
    COMPANY,
    CONDUCTOR,
    DELIVERY_CREW,
    PM_TOKEN,
    RUN_ID,
    WORKER_TOKEN,
    audit_events,
    ephemeral_service_account,
    http_client,
    mcp_session,
    register_sandbox,
)

from common.authn import ServiceAccount
from common.topology import COMPANY_NAMESPACE

pytestmark = pytest.mark.anyio

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
    async with http_client(app, WORKER_TOKEN) as client:
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
    assert event["agent_id"] == AGENT_ID and event["role"] == "implementer"
    assert event["lifecycle"] == "ephemeral" and event["parent_agent_id"] == CONDUCTOR
    assert event["run_id"] == RUN_ID
    assert event["decision"] == "allow"
    assert event["rule"] == "crew_feature_delivery_implementer_sandbox_read_file"
    assert event["backend_status"] == "ok"
    assert SHA256_HEX.match(event["args_digest"])

    # The decision input, enriched from the participants row: the run it
    # binds the caller to, its parent and its crew.
    decision_input = backends.policy_inputs[-1]
    assert decision_input["company"] == COMPANY
    assert decision_input["lifecycle"] == "ephemeral"
    assert decision_input["parent_agent_id"] == CONDUCTOR
    assert decision_input["crew_template"] == DELIVERY_CREW
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
        (
            "sandbox.git_bundle",
            {"run_id": RUN_ID, "branch": "feature/x", "message": "Add x"},
            "/bundle",
        ),
    ]
    async with mcp_session(app, AGENT_TOKEN) as session:
        for tool, args, _route in calls:
            result = await session.call_tool(tool, args)
            assert not result.isError, (tool, result.content)

    assert [route for route, _ in backends.sandbox_calls] == [
        route for _, _, route in calls
    ]
    exec_payload = next(
        dict(p) for route, p in backends.sandbox_calls if route == "/exec"
    )
    assert exec_payload == {"cmd": "ls", "timeout_s": 5}
    # The delivery commit's author is the verified caller, never an
    # argument: the policy input saw only what the agent sent.
    bundle_payload = next(p for route, p in backends.sandbox_calls if route == "/bundle")
    assert bundle_payload == {
        "branch": "feature/x",
        "message": "Add x",
        "author": AGENT_ID,
    }
    assert "author" not in backends.policy_inputs[-1]["args"]


# --- run binding ---------------------------------------------------------------


async def test_an_ephemeral_agent_naming_another_run_is_refused(app, backends, audit):
    """An ephemeral agent is bound to the one run its participants row
    names. Another run's live sandbox is no binding for it: the enrichment
    sets run_binding_verified false, the run-scoped rule cannot match, and
    the call is refused and audited before the sandbox hears of it."""
    other_run = "run-NOTMINE"
    await register_sandbox(app, RUN_ID)
    await register_sandbox(app, other_run)
    async with mcp_session(app, AGENT_TOKEN) as session:
        own = await session.call_tool(
            "sandbox.exec", {"run_id": RUN_ID, "cmd": "id", "timeout_s": 5}
        )
        assert not own.isError, own.content
        assert backends.policy_inputs[-1]["run_binding_verified"] is True
        foreign = await session.call_tool(
            "sandbox.exec", {"run_id": other_run, "cmd": "rm -rf /", "timeout_s": 5}
        )

    assert foreign.isError
    assert "denied by policy rule: default_deny" in foreign.content[0].text
    assert backends.policy_inputs[-1]["run_binding_verified"] is False
    assert [payload["cmd"] for _, payload in backends.sandbox_calls] == ["id"]

    event = audit_events(audit)[-1]
    assert event["run_id"] == other_run
    assert event["decision"] == "deny" and event["rule"] == "default_deny"
    assert event["backend_status"] is None
    assert SHA256_HEX.match(event["args_digest"])


@pytest.mark.parametrize(
    ("parent", "crew"),
    [("", DELIVERY_CREW), (CONDUCTOR, "")],
    ids=["no-parent", "no-crew"],
)
async def test_an_ephemeral_agent_without_parentage_is_refused_before_policy(
    app, backends, audit, participants, reviewer, parent, crew
):
    """An ephemeral identity carries the agent it was spawned under and its
    crew; a row missing either never reaches policy — refused, audited
    under `ephemeral_parentage_missing`, nothing proxied — while the bound
    implementer's same call goes through."""
    await register_sandbox(app, RUN_ID)
    orphan = "orphan-implementer-" + RUN_ID
    participants.add(
        orphan,
        RUN_ID,
        parent,
        crew,
        "implementer",
        company=COMPANY,
        namespace=COMPANY_NAMESPACE,
        service_account=ephemeral_service_account(orphan),
    )
    reviewer.accounts["orphan-secret"] = ServiceAccount(
        COMPANY_NAMESPACE, ephemeral_service_account(orphan)
    )
    args = {"run_id": RUN_ID, "path": "a.py"}
    async with mcp_session(app, AGENT_TOKEN) as session:
        assert not (await session.call_tool("sandbox.read_file", args)).isError
    asked = len(backends.policy_inputs)

    async with mcp_session(app, "orphan-secret") as session:
        refused = await session.call_tool("sandbox.read_file", args)

    assert refused.isError
    assert "denied by policy rule: ephemeral_parentage_missing" in refused.content[0].text
    assert len(backends.policy_inputs) == asked  # OPA was never asked
    assert len(backends.sandbox_calls) == 1
    event = audit_events(audit)[-1]
    assert event["agent_id"] == orphan and event["lifecycle"] == "ephemeral"
    assert event["decision"] == "deny"
    assert event["rule"] == "ephemeral_parentage_missing"


async def test_a_standing_agent_is_bound_only_to_a_run_in_its_own_namespace(
    app, backends, instances, reviewer
):
    """A standing caller's binding is a live registration in its own
    namespace: the conductor's claim on a run registered in the company
    namespace is verified, the same claim from a standing agent living in
    another namespace is not."""
    await register_sandbox(app, RUN_ID)
    instances.upsert("pm-elsewhere", COMPANY, "pm", "pm", namespace="elsewhere")
    reviewer.accounts["elsewhere-secret"] = ServiceAccount("elsewhere", "pm-elsewhere")

    verified = {}
    for token in (PM_TOKEN, "elsewhere-secret"):
        async with mcp_session(app, token) as session:
            await session.call_tool("sandbox.exec", {"run_id": RUN_ID, "cmd": "id"})
        decided = backends.policy_inputs[-1]
        verified[decided["agent_id"]] = decided["run_binding_verified"]
    assert verified == {CONDUCTOR: True, "pm-elsewhere": False}
    assert backends.sandbox_calls == []


async def test_terminated_registration_is_a_stale_binding(app, backends, audit):
    """The platform's binding to a run is the run's live registration: the
    gateway verifies it while the registration lives, and not once it is
    terminated. The compiled platform rule asks for no binding, so the
    platform's verify call on a terminated registration passes policy and
    fails at the backend: nothing reaches a sandbox, and the row records an
    allowed call whose backend failed."""
    await register_sandbox(app, RUN_ID)
    async with mcp_session(app, WORKER_TOKEN) as session:
        live = await session.call_tool("sandbox.run_tests", {"run_id": RUN_ID})
        assert not live.isError, live.content
        assert backends.policy_inputs[-1]["run_binding_verified"] is True

        async with http_client(app, WORKER_TOKEN) as client:
            resp = await client.request(
                "DELETE", "/admin/sandboxes", json={"run_id": RUN_ID}
            )
            assert resp.status_code == 200

        stale = await session.call_tool("sandbox.run_tests", {"run_id": RUN_ID})
    assert backends.policy_inputs[-1]["run_binding_verified"] is False
    assert stale.isError
    assert f"no sandbox registered for run_id {RUN_ID!r}" in stale.content[0].text
    assert [route for route, _ in backends.sandbox_calls] == ["/tests"]
    event = audit_events(audit)[-1]
    assert (event["tool"], event["run_id"]) == ("sandbox.run_tests", RUN_ID)
    assert event["decision"] == "allow"
    assert event["rule"] == "platform_sandbox_run_tests"
    assert event["backend_status"] == "error"


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
