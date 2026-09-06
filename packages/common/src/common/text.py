"""Text shapes more than one component must agree on."""

from collections.abc import Iterable

# The most of one untrusted text — a repository document, the sandbox's
# AGENTS.md — that reaches a model. Bounded because a file can be
# anything; the same bound on both sides so what the observation shows
# and what a crew reads are the same picture.
UNTRUSTED_CLIP = 8000


def clip_head(text: str, limit: int = UNTRUSTED_CLIP) -> str:
    """The head of `text`, with a marker saying how much was dropped —
    for a document, whose opening matters."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} chars clipped ...]\n"


def clip_tail(text: str, limit: int) -> str:
    """The tail of `text`, with a marker saying how much was dropped —
    for a test run's output, whose ending matters."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} chars clipped ...]\n" + text[-limit:]


def numbered_lines(lines: Iterable[str], first: int = 1) -> str:
    """`cat -n` numbering: a right-aligned line number, a tab, the line.
    The sandbox renders edit snippets with it and the agent host renders
    reads with it, so what the model anchors an edit on and what it gets
    back afterwards are the same picture."""
    return "".join(f"{first + i:6d}\t{line}" for i, line in enumerate(lines))
