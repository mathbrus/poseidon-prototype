"""The committed console interface's breaking-change comparison: a
removed field, a retyped one and a narrowed enum are breaking. That the
committed OpenAPI snapshot and golden examples are what the console
serves is CI's `scripts/console_interface.py --check`."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "console_interface.py"


@pytest.fixture(scope="module")
def interface():
    spec = importlib.util.spec_from_file_location("console_interface", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
