"""Text shapes the sandbox and the brain host must agree on."""

from collections.abc import Iterable


def numbered_lines(lines: Iterable[str], first: int = 1) -> str:
    """`cat -n` numbering: a right-aligned line number, a tab, the line.
    The sandbox renders edit snippets with it and the brain host renders
    reads with it, so what the model anchors an edit on and what it gets
    back afterwards are the same picture."""
    return "".join(f"{first + i:6d}\t{line}" for i, line in enumerate(lines))
