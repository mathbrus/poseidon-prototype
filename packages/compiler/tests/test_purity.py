"""The compiler provably performs no I/O beyond its inputs.

This is the property that makes "apply is the only mutation of enforcement"
checkable rather than aspirational: if compiling could touch a
cluster, write a file, or read something not passed to it, the blueprint
would stop being the source of truth and nobody could tell from the outside.
"""

import builtins
import socket

import pytest

from compiler import compile_blueprint


@pytest.fixture
def no_io(monkeypatch):
    """Make any file write or network access during compilation an error."""
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"compiler wrote to {file!r}")
        return real_open(file, mode, *args, **kwargs)

    def no_socket(*args, **kwargs):
        raise AssertionError("compiler opened a socket")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)


def test_compiling_writes_nothing_and_calls_nobody(blueprint, no_io):
    assert compile_blueprint(blueprint) is not None


def test_compiling_does_not_mutate_its_input(blueprint):
    """A caller must be able to compile twice, or compile a variant, without
    the first pass having quietly edited the document underneath it."""
    import copy

    original = copy.deepcopy(blueprint)
    compile_blueprint(blueprint)
    assert blueprint == original
