"""GatewayClient connection-failure behaviour.

A setup error must surface immediately, as itself: when the transport's
async generator is left to the garbage collector, anyio reports "attempted
to exit cancel scope in a different task" from an unrelated task and the
activity hangs until its start_to_close timeout — minutes of waiting for a
bug that is knowable in milliseconds.
"""

import asyncio
import contextlib

import pytest

from orchestrator.clients import GatewayClient, GatewayError


def transport(*, yields=None, raises: Exception | None = None):
    """Stand-in for `streamable_http_client`: an async context manager that
    either fails to connect or yields the given streams tuple."""

    @contextlib.asynccontextmanager
    async def factory(*args, **kwargs):
        if raises is not None:
            raise raises
        yield yields

    return factory


def connect(monkeypatch, factory) -> None:
    monkeypatch.setattr("orchestrator.clients.streamable_http_client", factory)


async def _attempt() -> None:
    async with GatewayClient("http://gateway.test:8080", "token"):
        pass


def test_connect_failure_raises_gateway_error_fast(monkeypatch):
    connect(monkeypatch, transport(raises=ConnectionRefusedError("no route")))

    async def scenario() -> None:
        with pytest.raises(GatewayError, match="gateway connection failed"):
            await _attempt()

    # The bound is the assertion: a wedged unwind would hang here.
    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_unexpected_stream_arity_fails_cleanly(monkeypatch):
    """The step-6 incident: a transport yielding a different tuple shape
    must surface as a plain gateway error, not as an anyio cancel-scope
    crash from whatever task the generator is finalized on."""
    connect(monkeypatch, transport(yields=("read", "write")))

    async def scenario() -> None:
        with pytest.raises(GatewayError, match="gateway connection failed"):
            await _attempt()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_connect_failure_releases_the_stack(monkeypatch):
    """The httpx client entered before the failure is closed, in this task."""
    closed: list[bool] = []

    class TrackingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> None:
            closed.append(True)

    monkeypatch.setattr("orchestrator.clients.httpx.AsyncClient", TrackingClient)
    connect(monkeypatch, transport(raises=ConnectionRefusedError("no route")))

    async def scenario() -> None:
        with pytest.raises(GatewayError):
            await _attempt()

    asyncio.run(scenario())
    assert closed == [True]


def test_spurious_cancellation_becomes_a_gateway_error(monkeypatch):
    """The transport runs its connection in an anyio task group, so an
    unreachable gateway can surface as this task cancelling itself. A
    compensation path that catches Exception must still see an error —
    otherwise teardown dies instead of finalizing the run."""
    connect(monkeypatch, transport(raises=asyncio.CancelledError()))

    async def scenario() -> None:
        with pytest.raises(GatewayError, match="gateway connection failed"):
            await _attempt()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_a_real_cancellation_is_still_a_cancellation(monkeypatch):
    """Cooperative cancel must keep working: when the workflow cancels the
    activity while it is connecting, the CancelledError belongs to the
    caller and must not be swallowed into a GatewayError."""

    @contextlib.asynccontextmanager
    async def slow_connect(*args, **kwargs):
        await asyncio.sleep(30)  # cancelled long before this returns
        yield ("read", "write", None)

    connect(monkeypatch, slow_connect)

    async def run() -> None:
        connecting = asyncio.ensure_future(_attempt())
        await asyncio.sleep(0.05)  # let it reach the await inside __aenter__
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting

    asyncio.run(run())
