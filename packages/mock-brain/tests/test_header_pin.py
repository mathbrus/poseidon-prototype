"""The mock keys its scripts on the agent host's procedure header. The two
packages spell it independently (the mock image carries no agent code), so
the spelling is pinned here rather than trusted."""

from agent.prompting import PROCEDURE_HEADER as HOST_HEADER
from mock_brain.brain import PROCEDURE_HEADER as MOCK_HEADER


def test_the_mock_reads_the_header_the_host_writes():
    assert MOCK_HEADER == HOST_HEADER
