"""The mock re-spells the platform's contract on purpose — a rename on
one side is caught by the other. This is where both spellings meet."""

from blueprint.catalog import CREW_TEMPLATES, STEWARDSHIP_CREW
from blueprint.documents import DOCUMENT_NAMES, document_header
from common.documents import BOOTSTRAP_TITLES
from common.tasks import crew_trailer
from mcp_gateway.service import ARCHITECTURE_REQUEST_TITLE_PREFIX
from mock_brain import brain
from orchestrator.activities.observation import decision_prompt


def test_the_document_headers_are_the_platforms():
    assert brain.STRATEGY_HEADER == document_header("strategy")
    assert brain.ARCHITECTURE_HEADER == document_header("architecture")
    assert set(DOCUMENT_NAMES) == {"strategy", "architecture"}


def test_the_ticket_titles_and_trailers_are_the_platforms():
    assert brain.BOOTSTRAP_STRATEGY_TITLE == BOOTSTRAP_TITLES["strategy"]
    assert brain.ARCHITECTURE_REQUEST_TITLE_PREFIX == ARCHITECTURE_REQUEST_TITLE_PREFIX
    assert brain.STEWARDSHIP_CREW_LINE == crew_trailer(STEWARDSHIP_CREW)
    assert brain.CONTRACT_CREW_LINE == crew_trailer("contract-authoring")
    assert "contract-authoring" in CREW_TEMPLATES


def test_the_observation_carries_the_lines_the_decide_script_parses():
    observed = {
        "active_runs": 0,
        "daily_spent_usd": 0.0,
        "open_tickets": 1,
        "backlog": [{"number": 5, "title": "Add slugify"}],
        "reviewable_prs": [{"number": 4, "url": "https://p/4"}],
        "recent_runs": [],
        "documents": {},
    }
    prompt = decision_prompt(observed, ["implement", "review"])
    for line in (
        brain.OPEN_TICKETS_SECTION,
        brain.REVIEWABLE_PRS_SECTION,
        brain.STARTABLE_LINE,
    ):
        assert line in prompt, line
    assert brain.OPEN_TICKETS_COUNT_RE.search(prompt).group(1) == "1"
