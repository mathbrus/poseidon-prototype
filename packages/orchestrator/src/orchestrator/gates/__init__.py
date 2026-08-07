"""Process gates — the explicit spine for human wait points in workflows.

The review workflow wires a real merge gate on this framework (configured
from the blueprint's compiled plan), and the implement workflow parks its
budget pause on the same primitives. The merge gate is the sole owner of
the merge guarantee: approvals are workflow state, and `git.merge_pr` is
only ever called after the gate approves.

Three pieces:
    Gate            — a generic signal-driven wait point for workflow code
    GateOutcome     — approved / rejected / timed_out (+ optional payload)
    gate_audit_event— shapes the `process-gate` audit event; an activity
                      writes it to the registry
"""

from collections.abc import Awaitable, Callable
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

    Workflow usage:

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
        on_escalate: Callable[[], Awaitable[None]] | None = None,
    ) -> GateOutcome:
        """Park until resolved or timed out. The timer/escalation primitive:
        when `escalate_after` elapses first, an `escalated` outcome is
        emitted to the audit log — and to `on_escalate` when the caller
        passes one (the review workflow records it as a real
        `process-gate` audit event through an activity) — and the wait
        continues to the deadline."""
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
        gate_audit_event(self._outcome, workflow_id=workflow.info().workflow_id)
        return self._outcome


def gate_audit_event(
    outcome: GateOutcome, workflow_id: str, run_id: str = ""
) -> dict[str, Any]:
    """A `source=process-gate` event in the audit ingestion shape.
    Shaped here, written by an *activity* — workflow
    code cannot touch the registry (Temporal sandbox). The
    `workflow` field carries the workflow id, which maps back to run_id
    even when the gate fires without one in hand."""
    import hashlib
    import json

    payload = {"gate": outcome.gate, "outcome": outcome.kind}
    event = {
        "ts": workflow.now().isoformat(),
        "run_id": run_id,
        "agent_id": "orchestrator",
        "role": "platform",
        "tool": f"gate.{outcome.gate}",
        "args_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "decision": "allow" if outcome.kind == "approved" else "deny",
        "rule": f"gate_{outcome.kind}",
        "latency_ms": 0,
        "backend_status": None,
        "source": "process-gate",
        "company": "",
        "lifecycle": "platform",
        "parent_agent_id": "",
        "workflow": workflow_id,
    }
    workflow.logger.info("process-gate outcome (audit stub): %s", event)
    return event
