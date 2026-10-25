"""The brain-of-record rule, pinned without a cluster: the mock runs
without ceremony, anything else refuses unless a person opted into
spending, and an unreadable brain refuses whatever the opt-in says. That
the suite's session guard reads the deployment is proven live, in
`tests/e2e_kind/test_brain_guard.py`."""

from .brain_guard import paid_brain_problem


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
