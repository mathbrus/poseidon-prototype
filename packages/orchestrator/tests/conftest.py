"""Shared fixtures for the orchestrator suite: worker settings pointed at
test doubles, the anyio backend, the Temporal time-skipping environment,
and a factory for gateway doubles."""

import pytest
from temporalio.testing import WorkflowEnvironment

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
        platform_namespace="poseidon-platform",
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        temporal_address="temporal.test:7233",
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        sandbox_image="poseidon-sandbox:abc123",
        agent_image="poseidon-agent:abc123",
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def env():
    """Temporal's time-skipping test environment. It fetches the test
    server binary on first use; a machine that cannot skips rather than
    failing, like the compiler's OPA decision tests."""
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:
        pytest.skip(f"temporal test server unavailable: {exc}")
    yield environment
    await environment.shutdown()


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
