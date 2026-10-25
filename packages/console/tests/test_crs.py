"""The Company CR the console submits: the one record the reconciler
reconciles and the platform reads back. What it carries is pinned here
against the two examples — the policies section the bounds are rendered
from among them."""

from pathlib import Path

import pytest
import yaml

from blueprint.bounds import render_bounds
from compiler import compile_blueprint
from console.crs import company_cr

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"


def spec_of(example: str) -> dict:
    document = yaml.safe_load((EXAMPLES / example).read_text())
    return company_cr(compile_blueprint(document))["spec"]


@pytest.mark.parametrize("example", ["hello-saas.yaml", "hello-saas-automated.yaml"])
def test_the_cr_carries_the_policies_section(example):
    spec = spec_of(example)
    assert spec["policies"]["branch_protection"]["protected"] == ["main"]
    assert spec["policies"]["deny_paths"] == [".github/workflows/"]


def test_the_bounds_render_from_the_submitted_spec():
    """The spec the console submits is the one the worker renders the
    bounds from, so both examples must render — the automated one
    naming its paths and its contract tree."""
    human = render_bounds(spec_of("hello-saas.yaml"))
    assert human.endswith("Merge: every pull request waits for a human's verdict.")
    automated = render_bounds(spec_of("hello-saas-automated.yaml"))
    assert "Contract tree: tests/contract/, written only by contract-author" in automated
    assert "every changed path is under docs/, slugify.py or tests/unit/" in automated


def test_the_cr_carries_the_blueprints_text_when_applied():
    """An apply writes the text a human applied beside the compiled spec;
    the compiled spec alone is not the blueprint."""
    plan = compile_blueprint(yaml.safe_load((EXAMPLES / "hello-saas.yaml").read_text()))
    assert "blueprintSource" not in company_cr(plan)["spec"]
    source = {
        "text": "company: {}\n",
        "sha256": "0" * 64,
        "appliedBy": "owner",
        "appliedAt": "2026-01-01T00:00:00+00:00",
    }
    assert company_cr(plan, source)["spec"]["blueprintSource"] == source
