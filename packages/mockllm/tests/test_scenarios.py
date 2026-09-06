"""The scenario map is complete: every marker the brain reads is mapped
to what carries it, which script reads it, and what the platform must
refuse — so a new scenario is a row here before it is a branch."""

from mockllm import brain


def test_every_marker_is_mapped():
    markers = {
        name
        for name in dir(brain)
        if name.endswith(("_MARKER", "_MARKERS")) and name.isupper()
    }
    assert markers == set(brain.SCENARIOS), markers ^ set(brain.SCENARIOS)
    for name, (carrier, scripts, platform) in brain.SCENARIOS.items():
        assert carrier and scripts and platform, name
