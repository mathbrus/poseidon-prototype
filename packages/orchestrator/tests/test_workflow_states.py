"""Every state a workflow's `state()` query can answer is one
`common.workflows` spells: the console passes it through as the
execution's state, and the console interface's enums are built from the
same sets, so a new literal here would break every client at once."""

import ast
from pathlib import Path

import pytest

import common.workflows as names
from common.workflows import HEARTBEAT_STATES, RUN_STATES

WORKFLOWS = Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "workflows"


def _leaves(node: ast.expr) -> list[ast.expr]:
    """The values an assignment can take: both arms of a conditional."""
    if isinstance(node, ast.IfExp):
        return _leaves(node.body) + _leaves(node.orelse)
    return [node]


def _value(node: ast.expr) -> object:
    """A leaf evaluated against `common.workflows` alone: a literal, a
    name it defines, or a subscript of one of its tables. Anything else
    is a state spelled somewhere else."""
    source = ast.unparse(node)
    return eval(source, {"__builtins__": {}}, vars(names))  # noqa: S307


def _assigned_states(path: Path) -> list[tuple[int, ast.expr]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Attribute) and t.attr == "_state" for t in targets):
            found.extend((node.lineno, leaf) for leaf in _leaves(value))
    return found


@pytest.mark.parametrize(
    "module, states",
    [
        ("base.py", RUN_STATES),
        ("scope.py", RUN_STATES),
        ("implement.py", RUN_STATES),
        ("review.py", RUN_STATES),
        ("heartbeat.py", HEARTBEAT_STATES),
    ],
)
def test_every_state_assignment_draws_from_common_workflows(module, states):
    assigned = _assigned_states(WORKFLOWS / module)
    assert assigned, f"{module} assigns no state"
    for lineno, leaf in assigned:
        assert not isinstance(leaf, ast.Constant), (
            f"{module}:{lineno} spells a state literally; name it in common.workflows"
        )
        assert _value(leaf) in states, f"{module}:{lineno}: {ast.unparse(leaf)}"


def test_the_workflow_modules_are_all_covered():
    covered = {"base.py", "scope.py", "implement.py", "review.py", "heartbeat.py"}
    on_disk = {p.name for p in WORKFLOWS.glob("*.py") if p.name != "__init__.py"}
    assert on_disk <= covered, sorted(on_disk - covered)
