"""The brain-of-record guardrail, pinned.

The refusal logic itself is pure (`paid_brain_problem`), so its edges are
asserted without a cluster; the live half — that the session-level fixture
actually reads the deployment — is exercised by every other module in this
suite, all of which run behind it.
"""

from .conftest import deployed_brain_image, paid_brain_problem


def test_the_mock_brain_runs_without_ceremony():
    assert paid_brain_problem("poseidon-mock-brain:abc123", None) is None
    assert paid_brain_problem("localhost:5001/poseidon-mock-brain:abc", None) is None


def test_only_the_mock_brain_image_exactly_is_the_mock():
    """The match is the repository name, not a substring: a lookalike or
    the image's retired name reads as a paid brain and refuses."""
    for image in (
        "poseidon-mock-brain-proxy:abc",
        "ghcr.io/berriai/litellm:poseidon-mock-brain",
        "poseidon-mockllm:abc123",
    ):
        assert paid_brain_problem(image, None) is not None, image


def test_a_paid_brain_refuses_without_the_opt_in():
    problem = paid_brain_problem("ghcr.io/berriai/litellm:v1.95.1", None)
    assert problem is not None
    assert "PAID brain" in problem
    assert "POSEIDON_ALLOW_PAID=1" in problem


def test_the_opt_in_is_exactly_one():
    """Only the explicit spelling opts in — truthy lookalikes do not."""
    image = "ghcr.io/berriai/litellm:v1.95.1"
    assert paid_brain_problem(image, "1") is None
    for almost in ("true", "yes", "0", ""):
        assert paid_brain_problem(image, almost) is not None, almost


def test_an_unreadable_brain_refuses_fail_closed():
    """Fail-safe defaults: the dangerous condition is the absent input."""
    problem = paid_brain_problem("", None)
    assert problem is not None
    assert "unverified brain" in problem
    # And the opt-in does not bypass verification: spending deliberately
    # still requires knowing what you are spending on.
    assert paid_brain_problem("", "1") is not None


def test_the_live_session_verified_a_mock_brain():
    """This test ran, so the autouse guard passed — which means the
    deployment really answered and really carries the mock (or a human
    opted into spending; either way, a decision was made, not defaulted)."""
    image = deployed_brain_image()
    assert image, "the guard should have failed the session before this test"
