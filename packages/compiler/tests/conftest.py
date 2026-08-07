import copy
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"
GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def blueprint() -> dict:
    return yaml.safe_load(HELLO_SAAS.read_text())


@pytest.fixture
def lean(blueprint: dict) -> dict:
    """The lean roster IS the checked-in blueprint."""
    return blueprint


@pytest.fixture
def bridge(blueprint: dict) -> dict:
    """A standing-executor shape, kept only to prove the strict
    lean-roster error refuses it."""
    bridge = copy.deepcopy(blueprint)
    bridge["departments"]["roles"]["backend-engineer"] = {
        "template": "backend-engineer", "count": 1, "model_tier": "frontier",
    }
    return bridge
