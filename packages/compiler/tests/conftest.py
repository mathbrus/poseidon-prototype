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
    """The post-firing-diff roster (plan step 19): PM only."""
    lean = copy.deepcopy(blueprint)
    del lean["departments"]["roles"]["backend-engineer"]
    del lean["budgets"]["per_role_monthly"]["backend-engineer"]
    return lean
