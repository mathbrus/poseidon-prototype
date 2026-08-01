"""Process-gate skeleton (§4.2) — the early, explicit spine for gates.

Ships at M1 with no gate wired into any workflow (M1 implement is gate-free
by design, overview §9). Step 20 configures real gates from the blueprint's
compiled plan; if O-1 lands as A, the merge gate built on this framework is
the sole owner of the merge guarantee, which is why the spine exists early.

Three pieces:
    Gate            — a generic signal-driven wait point for workflow code
    GateOutcome     — approved / rejected / timed_out (+ optional payload)
    gate_audit_event— stub for the `process-gate` audit source (§5.2); the
                      real ingestion contract lands at plan step 13
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from temporalio import workflow


@dataclass
class GateOutcome:
    gate: str
    kind: Literal["approved", "rejected", "timed_out", "escalated"]
    payload: str | None = None


class Gate:
    """A named wait point a workflow parks on until a signal or a deadline.

    Workflow usage (none at M1 — first wired at step 20):

        gate = Gate("merge")

        @workflow.signal
        def merge(self, approved: bool, payload: str | None = None) -> None:
            gate.resolve(approved, payload)

        outcome = await gate.wait(timeout=..., escalate_after=...)
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._outcome: GateOutcome | None = None

    def resolve(self, approved: bool, payload: str | None = None) -> None:
        if self._outcome is None:  # first signal wins
            self._outcome = GateOutcome(
                gate=self.name,
                kind="approved" if approved else "rejected",
                payload=payload,
            )

    async def wait(
        self,
        timeout: timedelta,
        escalate_after: timedelta | None = None,
    ) -> GateOutcome:
        """Park until resolved or timed out. The timer/escalation primitive:
        when `escalate_after` elapses first, an `escalated` outcome is
        emitted to the audit stub and the wait continues to the deadline."""
        remaining = timeout
        if escalate_after is not None and escalate_after < timeout:
            try:
                await workflow.wait_condition(
                    lambda: self._outcome is not None, timeout=escalate_after
                )
            except TimeoutError:
                gate_audit_event(
                    GateOutcome(gate=self.name, kind="escalated"),
                    workflow_id=workflow.info().workflow_id,
                )
                remaining = timeout - escalate_after
        if self._outcome is None:
            try:
                await workflow.wait_condition(
                    lambda: self._outcome is not None, timeout=remaining
                )
            except TimeoutError:
                self._outcome = GateOutcome(gate=self.name, kind="timed_out")
        gate_audit_event(self._outcome, workflow_id=workflow.info().workflow_id)
        return self._outcome


def gate_audit_event(outcome: GateOutcome, workflow_id: str) -> dict[str, Any]:
    """Stub for `source=process-gate` audit events (§5.2): shapes the event
    and logs it. Real emission onto the audit ingestion contract is plan
    step 13; gate outcomes become first-class audit events at step 20."""
    event = {
        "source": "process-gate",
        "gate": outcome.gate,
        "outcome": outcome.kind,
        "workflow_id": workflow_id,
    }
    workflow.logger.info("process-gate outcome (audit stub): %s", event)
    return event
