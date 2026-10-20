"""What a test run's exit code means. The agent host's advisory verify and
the orchestrator's gate of record must read the same answer from the same
exit, so the rule lives here once."""

# pytest's exit when it collected no tests at all: not a failure of any
# test, the absence of a suite.
NO_TESTS_COLLECTED = 5


def suite_green(exit_code: int | None, scoped: bool) -> bool:
    """Whether a test run passed. Exit 0 is green. A run that collected
    nothing is green only when it was unscoped — the whole tree as it
    stands — because a pull request cannot have weakened a suite that was
    already empty; a repository starts with no tests, and its first
    deliveries are documents. A scoped run names the paths the platform
    expects tests under (the contract tree), so collecting nothing from
    them is red: the tests the gate relies on are missing."""
    if exit_code == 0:
        return True
    return exit_code == NO_TESTS_COLLECTED and not scoped
