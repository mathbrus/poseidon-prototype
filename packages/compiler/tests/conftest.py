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
def standing_executor(blueprint: dict) -> dict:
    """The blueprint with an executor hired as a standing role — the shape
    the strict lean-roster rule refuses."""
    document = copy.deepcopy(blueprint)
    document["departments"]["roles"]["backend-engineer"] = {
        "template": "backend-engineer", "count": 1, "model_tier": "frontier",
    }
    return document
