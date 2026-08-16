"""The Responses surface of the mock: a wire-shape adapter over the same
brain. Requests arrive as `instructions` + `input` items with flattened tool
schemas; the brain must see the chat shape it keys phases on, and its
completion must come back as Responses output items the OpenHands SDK can
parse (`Message.from_llm_responses_output`)."""

import json

from fastapi.testclient import TestClient

from mockllm.app import create_app

client = TestClient(create_app())

PM_SYSTEM = "You are the product manager of an agent software company."
GOAL_ITEM = {
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "Goal: build a tiny Tetris"}],
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
        "name": "request_crew",
        "description": "record a crew request",
        "parameters": {"type": "object", "properties": {}},
    },
]


def post(body: dict, path: str = "/v1/responses") -> dict:
    resp = client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_planner_brief_is_a_completed_message_not_a_404():
    """No tools offered -> the planner phase; the brief comes back as one
    completed output message with usage in Responses vocabulary."""
    body = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": "You are the planner.",
            "input": [GOAL_ITEM],
        }
    )
    assert body["object"] == "response"
    assert body["status"] == "completed"
    (item,) = body["output"]
    assert item["type"] == "message" and item["role"] == "assistant"
    assert "slugify.py" in item["content"][0]["text"]
    usage = body["usage"]
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


def test_both_path_aliases_serve_the_same_surface():
    for path in ("/responses", "/v1/responses"):
        assert post({"model": "m", "input": "hello"}, path=path)["object"] == "response"


def test_flattened_tools_key_the_pm_phase_and_calls_come_back_as_items():
    """The PM script fires on the system prompt with Responses-shaped tools:
    the first completion is a create_issue function_call item the SDK's
    parser understands (call_id + JSON-string arguments)."""
    body = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [GOAL_ITEM],
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
    the stateless brain moves on to request_crew."""
    first = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [GOAL_ITEM],
            "tools": PM_TOOLS,
        }
    )
    (call,) = first["output"]
    followup = post(
        {
            "model": "gpt-5.6-luna",
            "instructions": PM_SYSTEM,
            "input": [
                GOAL_ITEM,
                call,
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": '{"url": "https://github.test/example/issues/1"}',
                },
            ],
            "tools": PM_TOOLS,
        }
    )
    (item,) = followup["output"]
    assert item["type"] == "function_call"
    assert item["name"] == "request_crew"


def test_chat_completions_surface_is_unchanged():
    """The e2e default path must not regress: mock-brain still speaks chat."""
    resp = client.post(
        "/chat/completions",
        json={
            "model": "mock-brain",
            "messages": [
                {"role": "system", "content": "You are the planner."},
                {"role": "user", "content": "Goal: anything"},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"
