"""The shared OPA decision transport: one endpoint, one decision path, one
fail-closed default for both authorization chokepoints that query OPA
directly (crew.request recording and conduct.start)."""

import pytest

from orchestrator import opa

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    last = None

    def __init__(self, payload):
        self._payload = payload

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json):
        _Http.last = {"url": url, "input": json["input"]}
        return _Resp(self._payload)


async def test_evaluate_posts_to_the_company_decision_path(monkeypatch):
    http = _Http({"result": {"allow": True, "rule": "role_pm_crew_request"}})
    monkeypatch.setattr(opa.httpx, "AsyncClient", http)
    result = await opa.evaluate_decision(
        "http://opa:8181/", "hello-saas", {"tool": "crew.request"}
    )
    assert result == {"allow": True, "rule": "role_pm_crew_request"}
    assert _Http.last["url"] == (
        "http://opa:8181/v1/data/poseidon/authz/hello_saas/decision"
    )
    assert _Http.last["input"] == {"tool": "crew.request"}


async def test_evaluate_fails_closed_when_no_policy_is_served(monkeypatch):
    monkeypatch.setattr(opa.httpx, "AsyncClient", _Http({"result": None}))
    result = await opa.evaluate_decision("http://opa:8181", "hello-saas", {})
    assert result == opa.NO_POLICY
    assert result["allow"] is False
