"""The activation read: OPA's own record of what it serves, read as a
plain answer — a revision, or nothing yet."""

import httpx

from poseidon_operator import opa

URL = "http://opa.test:8181"


def answering(monkeypatch, status: int, payload: dict | None = None) -> None:
    def fake_get(url, timeout):
        request = httpx.Request("GET", url)
        return httpx.Response(status, json=payload or {}, request=request)

    monkeypatch.setattr(opa.httpx, "get", fake_get)


def test_reports_the_activated_revision(monkeypatch):
    answering(monkeypatch, 200, {"result": "aa79d0c8f762e24c"})
    assert opa.activated_revision(URL, "hello-saas") == "aa79d0c8f762e24c"


def test_nothing_activated_is_none(monkeypatch):
    """OPA answers `{}` for a path with no document — a bundle it has never
    activated, or one it activated under another name."""
    answering(monkeypatch, 200, {})
    assert opa.activated_revision(URL, "hello-saas") is None


def test_an_unreachable_opa_is_not_live_yet(monkeypatch):
    def refusing(url, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(opa.httpx, "get", refusing)
    assert opa.activated_revision(URL, "hello-saas") is None


def test_a_failing_opa_is_not_live_yet(monkeypatch):
    answering(monkeypatch, 503, {"error": "starting"})
    assert opa.activated_revision(URL, "hello-saas") is None


def test_asks_the_bundle_path_by_name(monkeypatch):
    asked: list[str] = []

    def fake_get(url, timeout):
        asked.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(opa.httpx, "get", fake_get)
    opa.activated_revision(URL + "/", "platform")
    assert asked == [f"{URL}/v1/data/system/bundles/platform/manifest/revision"]
