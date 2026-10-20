"""Process gates — the explicit spine for human wait points in workflows.

The review workflow wires a real merge gate on this framework (configured
from the blueprint's compiled plan), and the implement workflow parks its
budget park on the same primitives. The merge gate is the sole owner of
the merge guarantee: approvals are workflow state, and `git.merge_pr` is
only ever called after the gate approves.

Two pieces: `Gate`, a signal-driven wait point for workflow code, and
`GateOutcome`, what it resolved to. The `process-gate` audit event for a
transition is written by an activity (`record_gate_outcome`) — workflow
code cannot touch the registry.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from temporalio import workflow


@dataclass
class GateOutcome:
    gate: str
    kind: Literal["approved", "rejected", "timed_out", "escalated"]


class Gate:
    """A named wait point a workflow parks on until a signal or a deadline.

    Workflow usage:

        gate = Gate("merge")

        @workflow.signal
        def merge(self, approved: bool) -> None:
            gate.resolve(approved)

        outcome = await gate.wait(timeout=..., escalate_after=...)
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._outcome: GateOutcome | None = None

    def resolve(self, approved: bool) -> None:
        """The verdict alone: the person's reason is on the console's audit
        row, which is the record the release reads back."""
        if self._outcome is None:  # first signal wins
            self._outcome = GateOutcome(
                gate=self.name, kind="approved" if approved else "rejected"
            )

    async def wait(
        self,
        timeout: timedelta,
        escalate_after: timedelta | None = None,
        on_escalate: Callable[[], Awaitable[None]] | None = None,
    ) -> GateOutcome:
        """Park until resolved or timed out. The timer/escalation primitive:
        when `escalate_after` elapses first, `on_escalate` runs (the
        workflows record it as a `process-gate` audit event through an
        activity) and the wait continues to the deadline."""
        remaining = timeout
        if escalate_after is not None and escalate_after < timeout:
            try:
                await workflow.wait_condition(
                    lambda: self._outcome is not None, timeout=escalate_after
                )
            except TimeoutError:
                workflow.logger.info("gate %s escalated", self.name)
                if on_escalate is not None:
                    await on_escalate()
                remaining = timeout - escalate_after
        if self._outcome is None:
            try:
                await workflow.wait_condition(
                    lambda: self._outcome is not None, timeout=remaining
                )
            except TimeoutError:
                self._outcome = GateOutcome(gate=self.name, kind="timed_out")
        workflow.logger.info("gate %s %s", self.name, self._outcome.kind)
        return self._outcome
