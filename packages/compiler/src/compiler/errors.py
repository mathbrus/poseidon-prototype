"""Compile failures.

The point of compiling is that a governance mistake fails here rather than
at the first tool call in production, so the message has to be good
enough to fix the blueprint from: what is wrong, where, and what is allowed
instead.
"""


class CompileError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"blueprint does not compile ({len(problems)} problem(s)):\n{body}"
        )
