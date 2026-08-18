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


# A coherent conduct section for the shipped example: the PM decides,
# implement is the one startable workflow, and the daily cap funds four
# $5 runs.
CONDUCT = {
    "conductor": "pm",
    "heartbeat_minutes": 30,
    "max_concurrent_runs": 1,
    "may_start": ["implement"],
    "daily_cap": {"usd": 20},
}


@pytest.fixture
def conducted(blueprint: dict) -> dict:
    """The blueprint with a standing heartbeat declared."""
    document = copy.deepcopy(blueprint)
    document["conduct"] = copy.deepcopy(CONDUCT)
    return document


@pytest.fixture
def standing_executor(blueprint: dict) -> dict:
    """The blueprint with an executor hired as a standing role — the shape
    the strict lean-roster rule refuses."""
    document = copy.deepcopy(blueprint)
    document["departments"]["roles"]["backend-engineer"] = {
        "template": "backend-engineer",
        "count": 1,
        "model_tier": "frontier",
    }
    return document
