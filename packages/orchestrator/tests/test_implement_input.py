"""The implement input contract: exactly one task source — a spec handed
in directly, or the ticket the company already filed. Sequencing (the
issue fetch before dispatch) is a workflow concern, proven live by the
kind suite."""

import asyncio

import pytest
from temporalio.exceptions import ApplicationError

from orchestrator.workflows.implement import (
    ImplementInput,
    ImplementWorkflow,
    issue_task,
)


def _run(input: ImplementInput) -> None:
    asyncio.run(ImplementWorkflow().run(input))


def test_both_task_sources_is_refused():
    with pytest.raises(ApplicationError, match="exactly one"):
        _run(ImplementInput(run_id="run-1", task_text="task", issue_number=42))


def test_no_task_source_is_refused():
    with pytest.raises(ApplicationError, match="exactly one"):
        _run(ImplementInput(run_id="run-1"))


def test_issue_task_carries_number_url_title_and_body():
    text = issue_task(42, "Add slugify", "spec body", "https://github.test/i/42")
    assert text == (
        "Implement issue #42 (https://github.test/i/42): Add slugify\n\nspec body"
    )
