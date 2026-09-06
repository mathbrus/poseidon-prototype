"""The CLI is a thin client: every command is one console call with the
token, its refusals are the console's, and nothing is decided here."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from poseidon_cli import ConsoleClient, ConsoleError, PolicyDenied
from poseidon_cli import main as cli

CONSOLE = "http://console.test"


class Console:
    """A scripted console: records every request, answers from a table."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.answers: dict[tuple[str, str], tuple[int, dict | list]] = {
            ("GET", "/whoami"): (200, {"human": "owner", "company": "hello-saas"}),
            ("POST", "/runs/run-1/gates/merge"): (
                200,
                {
                    "run_id": "run-1",
                    "gate": "merge",
                    "approved": True,
                    "principal": "owner",
                },
            ),
            ("POST", "/runs/run-2/gates/merge"): (
                403,
                {
                    "detail": "denied by policy: human_not_recognized",
                    "rule": "human_not_recognized",
                },
            ),
            ("GET", "/runs/run-9"): (422, {"detail": "no run 'run-9'"}),
            ("GET", "/now"): (200, {"now": "2026-01-01T00:00:00+00:00"}),
            ("POST", "/blueprint/preview"): (
                200,
                {"diff": "hello-saas: no changes", "plan": {}},
            ),
            ("POST", "/blueprint/apply"): (
                200,
                {
                    "company": "hello-saas",
                    "created": 0,
                    "updated": 1,
                    "rule": "blueprint_apply",
                },
            ),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            (
                request.method,
                request.url.path,
                body,
                request.headers.get("authorization", ""),
            )
        )
        status, payload = self.answers.get((request.method, request.url.path), (404, {}))
        return httpx.Response(status, json=payload)


@pytest.fixture
def console() -> Console:
    return Console()


@pytest.fixture
def client(console: Console) -> ConsoleClient:
    return ConsoleClient(
        CONSOLE, "t-owner", transport=httpx.MockTransport(console.handler)
    )


def test_every_call_carries_the_token(client, console):
    assert client.whoami()["human"] == "owner"
    assert client.now() == "2026-01-01T00:00:00+00:00"
    assert {auth for _, _, _, auth in console.requests} == {"Bearer t-owner"}


def test_a_verdict_is_one_call(client, console):
    result = client.resolve("run-1", "merge", True, "reviewed")
    assert result["principal"] == "owner"
    assert console.requests[-1][:3] == (
        "POST",
        "/runs/run-1/gates/merge",
        {"approved": True, "payload": "reviewed"},
    )


def test_a_policy_refusal_names_the_rule(client):
    with pytest.raises(PolicyDenied) as caught:
        client.resolve("run-2", "merge", True)
    assert caught.value.rule == "human_not_recognized"
    assert caught.value.status == 403


def test_other_refusals_carry_the_detail(client):
    with pytest.raises(ConsoleError) as caught:
        client.run("run-9")
    assert caught.value.status == 422
    assert "run-9" in caught.value.detail


def test_the_client_reads_its_environment(monkeypatch):
    monkeypatch.setenv("POSEIDON_CONSOLE_URL", "http://elsewhere:1/")
    monkeypatch.setenv("POSEIDON_TOKEN", "t")
    client = ConsoleClient.from_env()
    assert client.base_url == "http://elsewhere:1"
    assert client.token == "t"


# -- the commands -----------------------------------------------------------


@pytest.fixture
def runner(console: Console, monkeypatch) -> CliRunner:
    monkeypatch.setenv("POSEIDON_CONSOLE_URL", CONSOLE)
    monkeypatch.setenv("POSEIDON_TOKEN", "t-owner")
    original = cli.ConsoleClient.__init__

    def scripted(self, base_url=None, token=None, transport=None, timeout=120.0):
        original(self, base_url, token, httpx.MockTransport(console.handler), timeout)

    monkeypatch.setattr(cli.ConsoleClient, "__init__", scripted)
    return CliRunner()


def test_signal_prints_the_recorded_verdict(runner, console):
    result = runner.invoke(cli.app, ["signal", "run-1", "merge", "--payload", "ok"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["principal"] == "owner"
    assert console.requests[-1][2] == {"approved": True, "payload": "ok"}


def test_a_denied_signal_exits_with_the_rule(runner):
    result = runner.invoke(cli.app, ["signal", "run-2", "merge"])
    assert result.exit_code == 1
    assert "human_not_recognized" in result.output


def test_apply_previews_first_and_applies_with_yes(runner, console, tmp_path):
    blueprint = tmp_path / "company.yaml"
    blueprint.write_text("company: {name: hello-saas}\n")
    result = runner.invoke(cli.app, ["blueprint", "apply", str(blueprint), "--yes"])
    assert result.exit_code == 0, result.output
    assert [path for _, path, _, _ in console.requests] == [
        "/blueprint/preview",
        "/blueprint/apply",
    ]
    assert "applied hello-saas: 0 created, 1 updated (blueprint_apply)" in result.output


def test_without_a_token_the_cli_says_how_to_get_one(monkeypatch):
    monkeypatch.delenv("POSEIDON_TOKEN", raising=False)
    result = CliRunner().invoke(cli.app, ["whoami"])
    assert result.exit_code == 2
    assert "poseidon login" in result.output


def test_login_asks_the_api_server_for_a_console_token(monkeypatch):
    import types

    minted: list[tuple[str, str, list[str]]] = []

    class FakeCore:
        def create_namespaced_service_account_token(self, name, namespace, body):
            minted.append((name, namespace, body.spec.audiences))
            return types.SimpleNamespace(status=types.SimpleNamespace(token="tok-123"))

    import kubernetes

    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", FakeCore)
    result = CliRunner().invoke(cli.app, ["login", "owner"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "tok-123"
    assert minted == [("human-owner", "poseidon-platform", ["poseidon-console"])]
