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
def standing_implementer(blueprint: dict) -> dict:
    """The blueprint with an implementer hired as a standing agent — the shape
    the strict lean-standing rule refuses."""
    document = copy.deepcopy(blueprint)
    document["standing"]["implementer"] = {
        "role": "implementer",
        "count": 1,
    }
    return document
