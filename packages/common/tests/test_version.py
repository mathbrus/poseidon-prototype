"""The running version is whatever the image was stamped with, or
`unreleased` for a process built outside git or run from a checkout."""

from common.version import UNRELEASED, running_version


def test_the_stamp_is_the_answer(monkeypatch):
    monkeypatch.setenv("POSEIDON_VERSION", "v0.1.0+2026.10.21-3-gabc1234-dirty")
    assert running_version() == "v0.1.0+2026.10.21-3-gabc1234-dirty"


def test_no_stamp_is_unreleased(monkeypatch):
    monkeypatch.delenv("POSEIDON_VERSION", raising=False)
    assert running_version() == UNRELEASED
    monkeypatch.setenv("POSEIDON_VERSION", "  ")
    assert running_version() == UNRELEASED
