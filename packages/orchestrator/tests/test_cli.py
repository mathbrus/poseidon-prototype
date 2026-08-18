"""CLI workflow-family resolution: the ids the thin Temporal client
addresses. The run-shaped families key on run_id; the conduct heartbeat is
one standing workflow per company, so its argument is the company name."""

import pytest
import typer

from orchestrator.cli.main import _workflow_id


def test_run_shaped_families_key_on_run_id():
    assert _workflow_id("implement", "run-1") == "implement-run-1"
    assert _workflow_id("intake", "run-1") == "intake-run-1"
    assert _workflow_id("review", "run-1") == "review-run-1"


def test_conduct_keys_on_the_company():
    assert _workflow_id("conduct", "hello-saas") == "conduct-hello-saas"


def test_an_unknown_family_exits():
    with pytest.raises(typer.Exit):
        _workflow_id("deploy", "run-1")
