"""The console interface: what every console client builds on, held to
the rules that keep it one interface that breaks loudly.

- it imports pydantic, the standard library and `common`, nothing else —
  not the registry, Temporal, FastAPI, Kubernetes, the compiler, the
  blueprint package or the console;
- every model forbids extra fields;
- every closed set is the constants its writers spell;
- every route names its models from here.
"""

import ast
import subprocess
import sys
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from common import audit, topology, workflows
from common.console import ROUTES, enums, models, route

PACKAGE = Path(models.__file__).parent
# What may appear in the interface's import closure besides the standard
# library: pydantic and what pydantic itself pulls in, and `common`.
ALLOWED = {
    "common",
    "pydantic",
    "pydantic_core",
    "annotated_types",
    "typing_extensions",
    "typing_inspection",
}


def _imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_the_interface_imports_only_pydantic_the_stdlib_and_common(path):
    strangers = {
        root
        for root in _imported_roots(path)
        if root not in sys.stdlib_module_names and root not in ALLOWED
    }
    assert not strangers, f"{path.name} imports {sorted(strangers)}"


def test_the_interface_pulls_nothing_else_in_transitively():
    """The `common` modules it imports must keep the same promise: a
    client that imports the interface imports nothing it did not ask
    for."""
    probe = (
        "import sys; before = set(sys.modules); import common.console; "
        "print(' '.join(sorted({m.split('.')[0] for m in set(sys.modules) - before})))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.split()
    strangers = {
        root
        for root in loaded
        if root not in sys.stdlib_module_names
        and root not in ALLOWED
        and not root.startswith("_")
    }
    assert not strangers, sorted(strangers)


def _models() -> list[type[BaseModel]]:
    return [
        value
        for value in vars(models).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == models.__name__
    ]


@pytest.mark.parametrize("model", _models(), ids=lambda m: m.__name__)
def test_every_model_forbids_extra_fields(model):
    assert model.model_config.get("extra") == "forbid"


def test_an_internal_field_leaking_through_is_an_error():
    with pytest.raises(ValidationError, match="args_digest"):
        models.Problem.model_validate({"detail": "x", "args_digest": "d"})


@pytest.mark.parametrize(
    "enum, values",
    [
        (enums.RunStatus, workflows.RUN_STATUSES),
        (enums.RunState, workflows.RUN_STATES),
        (enums.HeartbeatState, workflows.HEARTBEAT_STATES),
        (enums.Gate, workflows.GATES),
        (enums.GateOutcome, workflows.GATE_OUTCOMES),
        (enums.TickOutcome, workflows.TICK_OUTCOMES),
        (enums.Workflow, workflows.WORKFLOWS),
        (enums.StartableWorkflow, workflows.HUMAN_STARTABLE),
        (enums.AuditSource, audit.SOURCES),
        (enums.Decision, audit.DECISIONS),
        (enums.CompanyPhase, topology.COMPANY_PHASES),
    ],
    ids=lambda v: getattr(v, "__name__", ""),
)
def test_every_closed_set_is_the_constants_its_writers_spell(enum, values):
    assert issubclass(enum, StrEnum)
    assert tuple(member.value for member in enum) == tuple(values)


def test_every_route_names_its_models_from_the_interface():
    interface = set(_models())
    for entry in ROUTES:
        assert entry.response in interface, entry
        assert entry.request is None or entry.request in interface, entry
    assert len({(r.method, r.path) for r in ROUTES}) == len(ROUTES)


def test_route_finds_one_entry():
    assert route("GET", "/now").response is models.Now
    with pytest.raises(KeyError):
        route("GET", "/nowhere")


def test_a_rationale_is_required_and_bounded():
    for request in (
        models.VerdictRequest,
        models.CancelRequest,
        models.PauseRequest,
        models.ResumeRequest,
        models.DeleteRequest,
    ):
        fields = {"approved": True} if request is models.VerdictRequest else {}
        with pytest.raises(ValidationError):
            request.model_validate(fields)
        with pytest.raises(ValidationError):
            request.model_validate({**fields, "rationale": ""})
        with pytest.raises(ValidationError):
            request.model_validate(
                {**fields, "rationale": "x" * (audit.RATIONALE_LIMIT + 1)}
            )
        assert request.model_validate({**fields, "rationale": "why"}).rationale == "why"
