"""The console the CLI's tests talk to answers what the real one answers:
the console interface's golden examples (`packages/console/examples/`),
rendered from the console itself. A change to the interface the CLI does
not follow fails here, in the same pull request."""

import copy
import json
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parents[2] / "console" / "examples"

_BY_ROUTE = {
    example["route"]: example
    for example in (json.loads(path.read_text()) for path in EXAMPLES.glob("*.json"))
}


def golden(route: str) -> Any:
    """The answer of `route` (`"GET /runs/{run_id}"`), a fresh copy to
    derive a variation from."""
    return copy.deepcopy(_BY_ROUTE[route]["response"])


def golden_answers() -> dict[tuple[str, str], tuple[int, Any]]:
    """Every example as `(method, concrete path) → (status, answer)`."""
    return {
        (example["route"].split()[0], example["request"]["path"]): (
            example["status"],
            copy.deepcopy(example["response"]),
        )
        for example in _BY_ROUTE.values()
    }
