import pytest

from common.verify import NO_TESTS_COLLECTED, suite_green


@pytest.mark.parametrize("scoped", [False, True])
def test_exit_zero_is_green_and_a_failure_is_red_either_way(scoped):
    assert suite_green(0, scoped)
    assert not suite_green(1, scoped)
    assert not suite_green(124, scoped)
    assert not suite_green(None, scoped)


def test_an_empty_suite_is_green_only_unscoped():
    """A tree with no tests cannot have been weakened by the change; the
    contract tree the platform names must hold the tests it relies on."""
    assert suite_green(NO_TESTS_COLLECTED, scoped=False)
    assert not suite_green(NO_TESTS_COLLECTED, scoped=True)
