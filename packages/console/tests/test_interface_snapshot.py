"""The committed console interface: the OpenAPI snapshot and the golden
examples are what the console serves, every example is a valid answer
of its route, and the breaking-change comparison calls a removed field,
a retyped one and a narrowed enum breaking."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from common.console import ROUTES

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "console_interface.py"


@pytest.fixture(scope="module")
def interface():
    spec = importlib.util.spec_from_file_location("console_interface", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_snapshot_and_examples_are_the_consoles(interface):
    assert interface.check() == 0


@pytest.mark.parametrize("entry", ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_every_route_has_a_valid_golden_example(interface, entry):
    example = json.loads((interface.EXAMPLES / interface.example_name(entry)).read_text())
    assert example["route"] == f"{entry.method} {entry.path}"
    entry.response.model_validate(example["response"])
    body = example["request"]["body"]
    if entry.request is not None and "blueprint" not in (body or {}):
        entry.request.model_validate(body)


@pytest.fixture(scope="module")
def snapshot(interface):
    return json.loads(interface.SNAPSHOT.read_text())


def test_an_unchanged_interface_is_unchanged(interface, snapshot):
    assert interface.compare(snapshot, copy.deepcopy(snapshot)) == ([], [])


def test_a_removed_or_retyped_field_is_breaking(interface, snapshot):
    changed = copy.deepcopy(snapshot)
    run_view = changed["components"]["schemas"]["RunView"]["properties"]
    run_view["work"] = run_view.pop("task")
    run_view["iterations"] = {"type": "string"}
    found, added = interface.compare(snapshot, changed)
    assert "RunView.task: removed" in found
    assert "RunView.iterations: retyped" in found
    assert "RunView.work: added" in added


def test_a_narrowed_enum_is_breaking_and_a_widened_one_is_not(interface, snapshot):
    narrowed = copy.deepcopy(snapshot)
    narrowed["components"]["schemas"]["RunStatus"]["enum"].remove("failed")
    found, _ = interface.compare(snapshot, narrowed)
    assert found == ["RunStatus: values removed ['failed']"]
    widened = copy.deepcopy(snapshot)
    widened["components"]["schemas"]["RunStatus"]["enum"].append("aborted")
    assert interface.compare(snapshot, widened) == (
        [],
        ["RunStatus: values added ['aborted']"],
    )


def test_a_removed_route_and_a_new_required_request_field_are_breaking(
    interface, snapshot
):
    changed = copy.deepcopy(snapshot)
    del changed["paths"]["/now"]
    request = changed["components"]["schemas"]["CancelRequest"]
    request["properties"]["ticket"] = {"type": "integer"}
    request["required"].append("ticket")
    found, _ = interface.compare(snapshot, changed)
    assert "route removed: GET /now" in found
    assert "CancelRequest.ticket: added, required" in found
