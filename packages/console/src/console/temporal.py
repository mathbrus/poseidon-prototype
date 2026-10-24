"""The console's Temporal client: start, signal, cancel and inspect the
workflows the orchestrator's worker hosts, and terminate and delete a
deleted company's.

Connects lazily and once; a request that finds Temporal unreachable
fails with `Unavailable`, which the app answers 503 — never a silent
success. The workflow ids, the task queue and the namespace are
`common.workflows`, shared with the worker, so the console can never
address a workflow the worker does not host.
"""

from typing import Any, Protocol

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from common.workflows import TASK_QUEUE, TEMPORAL_NAMESPACE, workflow_id


class Unavailable(Exception):
    """Temporal could not be reached."""


class WorkflowNotFound(Exception):
    """No workflow runs under that id."""


class AlreadyStarted(Exception):
    """A workflow already runs under that id."""


def _refusal(exc: RPCError, wf_id: str) -> Exception:
    """Temporal's NOT_FOUND is the one answer about the workflow; every
    other RPC failure is about Temporal."""
    if exc.status == RPCStatusCode.NOT_FOUND:
        return WorkflowNotFound(wf_id)
    return Unavailable(str(exc))


class Workflows(Protocol):
    """What the service needs from Temporal — a seam the tests fake."""

    async def start(self, workflow: str, key: str, payload: dict[str, Any]) -> str: ...
    async def signal(self, wf_id: str, name: str, args: list[Any]) -> None: ...
    async def cancel(self, wf_id: str) -> None: ...
    async def inspect(self, wf_id: str) -> dict[str, Any]: ...
    async def terminate(self, wf_id: str, reason: str) -> None: ...
    async def delete(self, wf_id: str) -> None: ...


class TemporalWorkflows:
    def __init__(self, address: str) -> None:
        self._address = address
        self._client: Client | None = None

    async def _connect(self) -> Client:
        if self._client is None:
            try:
                self._client = await Client.connect(
                    self._address, namespace=TEMPORAL_NAMESPACE
                )
            except Exception as exc:
                raise Unavailable(str(exc)) from exc
        return self._client

    async def start(self, workflow: str, key: str, payload: dict[str, Any]) -> str:
        """Start `workflow` under its deterministic id with a JSON payload;
        the worker decodes it into the workflow's input type."""
        client = await self._connect()
        wf_id = workflow_id(workflow, key)
        try:
            await client.start_workflow(
                workflow, payload, id=wf_id, task_queue=TASK_QUEUE
            )
        except WorkflowAlreadyStartedError as exc:
            raise AlreadyStarted(wf_id) from exc
        except RPCError as exc:
            raise Unavailable(str(exc)) from exc
        return wf_id

    async def signal(self, wf_id: str, name: str, args: list[Any]) -> None:
        client = await self._connect()
        try:
            await client.get_workflow_handle(wf_id).signal(name, args=args)
        except RPCError as exc:
            raise _refusal(exc, wf_id) from exc

    async def cancel(self, wf_id: str) -> None:
        client = await self._connect()
        try:
            await client.get_workflow_handle(wf_id).cancel()
        except RPCError as exc:
            raise _refusal(exc, wf_id) from exc

    async def terminate(self, wf_id: str, reason: str) -> None:
        """End a running workflow at once, without its own cleanup."""
        client = await self._connect()
        try:
            await client.get_workflow_handle(wf_id).terminate(reason=reason)
        except RPCError as exc:
            raise _refusal(exc, wf_id) from exc

    async def delete(self, wf_id: str) -> None:
        """Delete every execution under `wf_id` from Temporal's history and
        visibility: each run of a continue-as-new chain, the current one
        first, since the listing may not show it yet. Temporal terminates
        one still running. Deleting what is already gone is nothing."""
        client = await self._connect()
        try:
            run_ids = [""] + [
                execution.run_id
                async for execution in client.list_workflows(f"WorkflowId = '{wf_id}'")
            ]
        except RPCError as exc:
            raise Unavailable(str(exc)) from exc
        for run_id in run_ids:
            request = DeleteWorkflowExecutionRequest(
                namespace=TEMPORAL_NAMESPACE,
                workflow_execution=WorkflowExecution(workflow_id=wf_id, run_id=run_id),
            )
            try:
                await client.workflow_service.delete_workflow_execution(request)
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise Unavailable(str(exc)) from exc

    async def inspect(self, wf_id: str) -> dict[str, Any]:
        """The workflow's execution status and, while it runs, its
        API-visible phase (`state`): a parked gate must be a visible
        pending state, never a silent hang."""
        client = await self._connect()
        handle = client.get_workflow_handle(wf_id)
        try:
            description = await handle.describe()
        except RPCError as exc:
            raise _refusal(exc, wf_id) from exc
        if description.status is None:
            raise Unavailable(f"temporal described {wf_id} without a status")
        status = description.status.name
        state = None
        if description.status == WorkflowExecutionStatus.RUNNING:
            try:
                state = await handle.query("state")
            except Exception:
                state = None
        return {"workflow_id": wf_id, "status": status, "state": state}
