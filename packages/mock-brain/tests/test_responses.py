"""The Responses surface of the mock: a wire-shape adapter over the same
brain. Requests arrive as `instructions` + `input` items with flattened tool
schemas; the brain must see the chat shape it keys procedures on, and its
completion must come back as Responses output items the OpenHands SDK can
parse (`Message.from_llm_responses_output`)."""

import json

import pytest
from fastapi.testclient import TestClient

from mock_brain.app import COST_PER_1K_TOKENS, create_app

client = TestClient(create_app())

PM_SYSTEM = "# Poseidon procedure: scope\nYou are the product manager."
OBSERVATION_ITEM = {
    "type": "message",
    "role": "user",
    "content": [
        {
            "type": "input_text",
            "text": "Scope the next slice.\n\nStrategy:\nBuild a tiny Tetris (NEXT-SLICE-OPEN).",
        }
    ],
}
# Responses tool schemas are flat — no "function" wrapper.
PM_TOOLS = [
    {
        "type": "function",
        "name": "create_issue",
        "description": "file a ticket",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "comment_issue",
        "description": "comment a ticket",
        "parameters": {"type": "object", "properties": {}},
    },
]


def post(body: dict, path: str = "/v1/responses") -> dict:
    resp = client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


DECIDE_ITEM = {
    "type": "message",
    "role": "user",
    "content": [
        {
            "type": "input_text",
            "text": (
                "Heartbeat tick. Runs in flight: 0. Spent today: $0.00."
                " Open tickets on the repository: 0.\n"
                "Startable actions: implement, review, idle\n\n"
                "Open tickets:\n(none)\n\nDelivered pull requests awaiting review:\n"
                "(none)\n\nRecent runs:\n(none)"
            ),
        }
    ],
}


def test_a_toolless_completion_is_a_completed_message_not_a_404():
    """No tools offered and a decide prompt -> the decision comes back as
    one completed output message with usage in Responses vocabulary."""
    body = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": "# Poseidon procedure: decide\nChoose the next action.",
            "input": [DECIDE_ITEM],
        }
    )
    assert body["object"] == "response"
    assert body["status"] == "completed"
    (item,) = body["output"]
    assert item["type"] == "message" and item["role"] == "assistant"
    assert "DECISION: idle" in item["content"][0]["text"]
    usage = body["usage"]
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["input_tokens_details"]["cached_tokens"] == usage["input_tokens"] // 2
    assert (
        usage["output_tokens_details"]["reasoning_tokens"] == usage["output_tokens"] // 2
    )


def test_both_surfaces_report_their_cost_in_the_litellm_header():
    """The gateway meters spend from `x-litellm-response-cost`; the mock,
    impersonating the proxy, must report a deterministic cost derived from
    its own usage on every completion — chat and Responses alike."""
    for path, in_key, out_key, body in (
        (
            "/v1/chat/completions",
            "prompt_tokens",
            "completion_tokens",
            {"model": "mock-brain", "messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            "/v1/responses",
            "input_tokens",
            "output_tokens",
            {"model": "mock-brain", "input": "hi"},
        ),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 200, resp.text
        usage = resp.json()["usage"]
        expected = (usage[in_key] + usage[out_key]) / 1000 * COST_PER_1K_TOKENS
        assert float(resp.headers["x-litellm-response-cost"]) == pytest.approx(expected)


def test_both_path_aliases_serve_the_same_surface():
    for path in ("/responses", "/v1/responses"):
        assert post({"model": "m", "input": "hello"}, path=path)["object"] == "response"


def test_flattened_tools_key_the_scope_procedure_and_calls_come_back_as_items():
    """The PM script fires on the system prompt with Responses-shaped tools:
    the first completion is a create_issue function_call item the SDK's
    parser understands (call_id + JSON-string arguments)."""
    body = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [OBSERVATION_ITEM],
            "tools": PM_TOOLS,
        }
    )
    (item,) = body["output"]
    assert item["type"] == "function_call"
    assert item["name"] == "create_issue"
    assert item["call_id"] and item["id"]
    args = json.loads(item["arguments"])
    assert args["title"].startswith("Implement:")


def test_function_call_history_advances_the_script():
    """Round-trip: echoing the prior function_call and its
    function_call_output back as input items must count as a made call, so
    the stateless brain moves on to the summary comment."""
    first = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [OBSERVATION_ITEM],
            "tools": PM_TOOLS,
        }
    )
    (call,) = first["output"]
    followup = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [
                OBSERVATION_ITEM,
                call,
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": '{"number": 1, "url": "https://github.test/example/issues/1"}',
                },
            ],
            "tools": PM_TOOLS,
        }
    )
    (item,) = followup["output"]
    assert item["type"] == "function_call"
    assert item["name"] == "comment_issue"


def test_chat_completions_surface_is_unchanged():
    """The e2e default path must not regress: mock-brain still speaks chat."""
    resp = client.post(
        "/chat/completions",
        json={
            "model": "mock-brain",
            "messages": [
                {"role": "system", "content": "# Poseidon procedure: implement\n…"},
                {"role": "user", "content": "Task: anything"},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"
