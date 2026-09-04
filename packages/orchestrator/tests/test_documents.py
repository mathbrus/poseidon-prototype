"""The company's documents as the platform reads them: at base_ref, under
the caller's run id, clipped, absent as a first-class state."""

import asyncio

from common.text import UNTRUSTED_CLIP
from orchestrator.activities.documents import (
    ABSENT,
    BOOTSTRAP_TITLES,
    bootstrap_body,
    present_texts,
    read_documents,
    sections,
)

REPO = {"url": "github.com/example/repo", "base_ref": "main"}
PATHS = {"strategy": "docs/STRATEGY.md", "architecture": "docs/ARCHITECTURE.md"}


class FakeGateway:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.reads: list[tuple[str, str, str, str]] = []

    async def read_document(self, run_id, repo, ref, path):
        self.reads.append((run_id, repo, ref, path))
        text = self.files.get(path)
        return {"present": text is not None, "text": text}


def test_documents_are_read_at_base_ref_under_the_run():
    gateway = FakeGateway({"docs/STRATEGY.md": "# Strategy\n"})
    documents = asyncio.run(read_documents(gateway, "run-1", REPO, PATHS))
    assert documents == {
        "strategy": {"path": "docs/STRATEGY.md", "present": True, "text": "# Strategy\n"},
        "architecture": {"path": "docs/ARCHITECTURE.md", "present": False, "text": None},
    }
    assert gateway.reads == [
        ("run-1", "github.com/example/repo", "main", "docs/STRATEGY.md"),
        ("run-1", "github.com/example/repo", "main", "docs/ARCHITECTURE.md"),
    ]


def test_a_long_document_is_clipped_before_any_model_sees_it():
    gateway = FakeGateway({"docs/STRATEGY.md": "x" * (UNTRUSTED_CLIP + 500)})
    documents = asyncio.run(read_documents(gateway, "run-1", REPO, PATHS))
    text = documents["strategy"]["text"]
    assert text.startswith("x" * UNTRUSTED_CLIP) and "500 chars clipped" in text


def test_sections_state_absence_and_dispatches_carry_only_present_texts():
    documents = {
        "strategy": {"path": "docs/STRATEGY.md", "present": True, "text": "Ship slugs."},
        "architecture": {"path": "docs/ARCHITECTURE.md", "present": False, "text": None},
    }
    assert sections(documents) == [
        "Strategy:\nShip slugs.",
        f"Architecture masterplan:\n{ABSENT}",
    ]
    assert present_texts(documents) == {"strategy": "Ship slugs."}


def test_the_bootstrap_ticket_is_the_stewards_whole_brief():
    """The platform-filed ticket names the file, the branch and the crew:
    the steward reads tickets, not this module."""
    assert BOOTSTRAP_TITLES == {
        "strategy": "Bootstrap: strategy",
        "architecture": "Bootstrap: architecture masterplan",
    }
    body = bootstrap_body("strategy", "docs/STRATEGY.md", "feature/strategy-abc123")
    assert "`docs/STRATEGY.md`" in body
    assert "`feature/strategy-abc123`" in body
    assert body.endswith("Crew: stewardship")
    assert "written once" in body
    assert "repository tree" in bootstrap_body("architecture", "docs/A.md", "feature/a")
