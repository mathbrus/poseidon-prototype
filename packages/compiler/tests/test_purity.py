"""The compiler provably performs no I/O beyond its inputs.

This is the property that makes "apply is the only mutation of enforcement"
checkable rather than aspirational: if compiling could touch a
cluster, write a file, or read something not passed to it, the blueprint
would stop being the source of truth and nobody could tell from the outside.
"""

import copy
import os
import socket
import subprocess
import sys
from contextlib import contextmanager

import pytest

from compiler import compile_blueprint

# An `open` whose mode or flags would change the file.
WRITE_MODES = frozenset("wax+")
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# Audit events that start a process, reach the network, or change the
# filesystem by a route other than `open`.
FORBIDDEN = (
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "socket.",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.truncate",
    "os.link",
    "os.symlink",
    "os.chmod",
    "os.chown",
    "os.utime",
    "shutil.",
)


def _forbidden(event: str, args: tuple) -> bool:
    if event == "open":
        _, mode, flags = args
        if isinstance(mode, str):
            return bool(WRITE_MODES & set(mode))
        return bool(flags & WRITE_FLAGS)
    return event.startswith(FORBIDDEN)


class Guard:
    """Refuses, and records, every write, process and connection made while
    a test watches. Python cannot remove an audit hook once added, so the
    hook is added once and is inert outside `watching()`."""

    def __init__(self) -> None:
        self.seen: list[str] | None = None

    def __call__(self, event: str, args: tuple) -> None:
        if self.seen is None or not _forbidden(event, args):
            return
        # Recorded as well as raised: a broad `except` in the code under
        # test cannot hide the attempt.
        self.seen.append(f"{event} {args!r}")
        raise AssertionError(f"compiling must not {event} {args!r}")

    @contextmanager
    def watching(self):
        self.seen = []
        try:
            yield self.seen
        finally:
            self.seen = None


GUARD = Guard()
sys.addaudithook(GUARD)


def test_compiling_writes_nothing_and_calls_nobody(blueprint):
    with GUARD.watching() as seen:
        plan = compile_blueprint(blueprint)
    assert plan is not None
    assert seen == []


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param(lambda d: (d / "f").write_text("x"), id="Path.write_text"),
        pytest.param(lambda d: open(d / "f", "a"), id="open-to-append"),
        pytest.param(lambda d: os.open(d / "f", os.O_WRONLY | os.O_CREAT), id="os.open"),
        pytest.param(lambda d: os.mkdir(d / "f"), id="os.mkdir"),
        pytest.param(lambda d: subprocess.run(["true"]), id="subprocess"),
        pytest.param(lambda d: socket.create_connection(("127.0.0.1", 9)), id="socket"),
    ],
)
def test_the_guard_refuses_every_way_out(tmp_path, escape):
    """What makes the test above able to fail: each way a compile could
    write a file, start a process or reach the network is refused before
    it happens, and recorded."""
    with GUARD.watching() as seen:
        with pytest.raises(AssertionError, match="compiling must not"):
            escape(tmp_path)
    assert len(seen) == 1
    assert not (tmp_path / "f").exists()


def test_compiling_does_not_mutate_its_input(blueprint):
    """A caller must be able to compile twice, or compile a variant, without
    the first pass having quietly edited the document underneath it."""
    original = copy.deepcopy(blueprint)
    compile_blueprint(blueprint)
    assert blueprint == original
