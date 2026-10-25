"""The brain-of-record guard reads the deployment.

The refusal rule itself is pure and pinned without a cluster
(`tests/test_brain_guard.py`); what only the live suite shows is that the
session-level guard asked the cluster what brain it deploys — every other
module in this suite runs behind it.
"""

from .conftest import deployed_brain_image


def test_the_live_session_verified_a_mock_brain():
    """This test ran, so the autouse guard passed — which means the
    deployment really answered and really carries the mock (or a human
    opted into spending; either way, a decision was made, not defaulted)."""
    image = deployed_brain_image()
    assert image, "the guard should have failed the session before this test"
