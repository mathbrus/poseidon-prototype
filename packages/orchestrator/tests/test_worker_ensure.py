"""The worker's heartbeat ensure pass: starts the heartbeat exactly when the
applied blueprint declares one, and treats every failure as a next-pass
retry — the loop only ever starts, never kills."""

from typing import Any

import pytest
from conftest import heartbeat_spec
from temporalio.exceptions import WorkflowAlreadyStartedError

from orchestrator.config import WorkerSettings
from orchestrator.worker.__main__ import ensure_heartbeat_once
from registry.memory import InMemoryCompanies

pytestmark = pytest.mark.anyio

COMPANY = "hello-saas"


def anyio_backend() -> str:
    return "asyncio"


class FakeTemporal:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[dict[str, Any]] = []

    async def start_workflow(self, name, input, *, id, task_queue) -> None:
        if self.error:
            raise self.error
        self.started.append(
            {"name": name, "input": input, "id": id, "task_queue": task_queue}
        )


class Quiet:
    def info(self, *a, **k) -> None: ...


def settings(**overrides: Any) -> WorkerSettings:
    return WorkerSettings(
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
        agent_image="poseidon-agent:abc123",
        company=COMPANY,
        **overrides,
    )


async def test_no_section_starts_nothing():
    client, companies = FakeTemporal(), InMemoryCompanies()
    companies.upsert(COMPANY, {"workflows": []})
    await ensure_heartbeat_once(client, settings(), companies, Quiet())
    assert client.started == []


async def test_declared_section_starts_the_heartbeat():
    client, companies = FakeTemporal(), InMemoryCompanies()
    companies.upsert(COMPANY, {"heartbeat": heartbeat_spec()})
    await ensure_heartbeat_once(client, settings(), companies, Quiet())
    (call,) = client.started
    assert call["name"] == "heartbeat"
    assert call["id"] == f"heartbeat-{COMPANY}"
    # No override: the blueprint's declared cadence stands.
    assert call["input"].pending_wake is False
    assert call["input"].timeouts is None


async def test_already_beating_is_the_normal_case():
    client = FakeTemporal(error=WorkflowAlreadyStartedError("heartbeat-x", "heartbeat"))
    companies = InMemoryCompanies()
    companies.upsert(COMPANY, {"heartbeat": heartbeat_spec()})
    await ensure_heartbeat_once(client, settings(), companies, Quiet())  # no raise


async def test_a_hiccup_is_swallowed_for_the_next_pass():
    client = FakeTemporal(error=RuntimeError("temporal unreachable"))
    companies = InMemoryCompanies()
    companies.upsert(COMPANY, {"heartbeat": heartbeat_spec()})
    await ensure_heartbeat_once(client, settings(), companies, Quiet())  # no raise
