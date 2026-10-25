"""Shared fixtures for the orchestrator suite: worker settings pointed at
test doubles, the anyio backend, the Temporal time-skipping environment,
and a factory for gateway doubles."""

import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from common.topology import PLATFORM_NAMESPACE
from orchestrator import clients
from orchestrator.config import WorkerSettings


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    """Dispatch polling at test speed."""
    monkeypatch.setattr(clients, "POLL_INTERVAL_S", 0.01)


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        opa_url="http://opa.test:8181",
        platform_namespace=PLATFORM_NAMESPACE,
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        temporal_address="temporal.test:7233",
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        sandbox_image="poseidon-sandbox:abc123",
        agent_image="poseidon-agent-host:abc123",
        model="mock-brain",
    )


def heartbeat_spec(**overrides) -> dict:
    """A compiled heartbeat section (the plan's shape, not the blueprint's)."""
    return {
        "conductor": "pm",
        "reconcile_minutes": 30,
        "max_concurrent_runs": 1,
        "max_attempts": 2,
        "max_open_tickets": 5,
        "may_start": ["implement"],
        "daily_cap_usd": 20.0,
        **overrides,
    }


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Module-scoped: anyio needs it at least as wide as `env`."""
    return "asyncio"


@contextlib.asynccontextmanager
async def _time_skipping():
    """A time-skipping test server. It fetches the test server binary on
    first use; a machine that cannot skips rather than failing, like the
    compiler's OPA decision tests."""
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:
        pytest.skip(f"temporal test server unavailable: {exc}")
    try:
        yield environment
    finally:
        await environment.shutdown()


@pytest.fixture(scope="module")
async def env():
    """Temporal's time-skipping test environment, one server per module.
    Its tests keep apart on their own uuid task queues and workflow ids,
    but they share its clock: the server skips time only while no
    activity is in flight, so a test that leaves one behind stalls every
    test after it until that activity times out. Such a test takes
    `own_env`."""
    async with _time_skipping() as environment:
        yield environment


@pytest.fixture
async def own_env():
    """A time-skipping server for one test: the one that leaves an
    activity in flight — a run cancelled mid-dispatch — whose clock no
    other test may wait on."""
    async with _time_skipping() as environment:
        yield environment


async def until(condition: Callable[[], Any], what: str) -> None:
    """Poll `condition` — plain or async — until it holds. Queries and
    signals do not advance the time-skipping clock, so this waits in real
    time, for at most ten seconds: a run that never gets there fails here,
    by name, instead of hanging the suite."""
    for _ in range(200):
        held = condition()
        if inspect.isawaitable(held):
            held = await held
        if held:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting until {what}")


async def state_is(handle: WorkflowHandle, state: str) -> bool:
    """Whether the workflow's `state` query answers `state`."""
    return await handle.query("state") == state


@pytest.fixture
def fake_gateway():
    """A factory for `GatewayClient` doubles: `fake_gateway(read_pr=fn, ...)`
    is a class whose instances are async context managers with the given
    coroutine functions as methods, ready to monkeypatch over the
    activity module's `GatewayClient`."""

    def make(**methods):
        async def aenter(self):
            return self

        async def aexit(self, *exc):
            return False

        namespace = {
            "__init__": lambda self, *a, **k: None,
            "__aenter__": aenter,
            "__aexit__": aexit,
            **methods,
        }
        return type("FakeGateway", (), namespace)

    return make
