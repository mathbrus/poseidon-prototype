"""The CLI is a thin client: every command is one console call with the
token, its refusals are the console's, and nothing is decided here."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from common.topology import PLATFORM_NAMESPACE
from poseidon_cli import ConsoleClient, ConsoleError, PolicyDenied
from poseidon_cli import main as cli

CONSOLE = "http://console.test"


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """A config directory of the test's own, so no saved token of the
    person running the suite reaches a test."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


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
            ("GET", "/runs"): (
                200,
                [
                    {"run_id": "run-1", "status": "running"},
                    {"run_id": "run-0", "status": "done"},
                ],
            ),
            ("DELETE", "/company"): (
                200,
                {"company": "hello-saas", "runs_cancelled": ["run-1"], "elapsed_s": 3},
            ),
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
                    "generation": 3,
                },
            ),
            ("GET", "/company"): (
                200,
                {
                    "company": "hello-saas",
                    "namespace": "poseidon-company",
                    "generation": 3,
                    "phase": "converged",
                    "observedGeneration": 3,
                    "bundleRevision": "r3",
                    "message": "",
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
        {"approved": True, "reason": "reviewed"},
    )


def test_a_policy_refusal_names_the_rule(client):
    with pytest.raises(PolicyDenied) as caught:
        client.resolve("run-2", "merge", True, "fine by me")
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
    assert client.token == "t" and client.token_source == "environment"


def test_the_client_falls_back_to_the_saved_token(monkeypatch, config_home):
    monkeypatch.delenv("POSEIDON_TOKEN", raising=False)
    path = config_home / "poseidon" / "token"
    path.parent.mkdir()
    path.write_text("saved\n")
    client = ConsoleClient.from_env()
    assert client.token == "saved" and client.token_source == "file"
    # The environment wins over the file when both are set.
    monkeypatch.setenv("POSEIDON_TOKEN", "from-env")
    assert ConsoleClient.from_env().token == "from-env"


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
    result = runner.invoke(cli.app, ["signal", "run-1", "merge", "--reason", "ok"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["principal"] == "owner"
    assert console.requests[-1][2] == {"approved": True, "reason": "ok"}


def test_a_verdict_needs_a_reason(runner, console):
    result = runner.invoke(cli.app, ["signal", "run-1", "merge"])
    assert result.exit_code == 2 and "--reason" in result.output
    assert console.requests == []


def test_a_denied_signal_exits_with_the_rule(runner):
    result = runner.invoke(cli.app, ["signal", "run-2", "merge", "--reason", "ok"])
    assert result.exit_code == 1
    assert "human_not_recognized" in result.output


def test_company_delete_names_the_running_runs_then_asks_the_console(runner, console):
    result = runner.invoke(cli.app, ["company", "delete", "--yes", "--reason", "done"])
    assert result.exit_code == 0, result.output
    assert "hello-saas: 1 running run(s) (run-1)" in result.output
    assert json.loads(result.output[result.output.index("{") :])["runs_cancelled"] == [
        "run-1"
    ]
    assert console.requests[-1][:3] == ("DELETE", "/company", {"reason": "done"})


def test_company_delete_asks_before_acting(runner, console):
    result = runner.invoke(cli.app, ["company", "delete"], input="n\n")
    assert result.exit_code != 0
    assert ("DELETE", "/company") not in [(m, p) for m, p, _, _ in console.requests]


def test_apply_waits_for_converged_and_says_so(runner, console, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    blueprint = tmp_path / "company.yaml"
    blueprint.write_text("company: {name: hello-saas}\n")
    # The first read still says activating for an older generation.
    stale = dict(
        console.answers[("GET", "/company")][1], phase="activating", observedGeneration=2
    )
    answers = iter([(200, stale), console.answers[("GET", "/company")]])
    original = console.handler

    def handler(request):
        if request.url.path == "/company":
            status, payload = next(answers)
            console.requests.append(("GET", "/company", None, ""))
            return httpx.Response(status, json=payload)
        return original(request)

    monkeypatch.setattr(console, "handler", handler)
    result = runner.invoke(cli.app, ["blueprint", "apply", str(blueprint), "--yes"])
    assert result.exit_code == 0, result.output
    assert "activating" in result.output and "converged (" in result.output
    assert [path for _, path, _, _ in console.requests].count("/company") == 2


def test_apply_gives_up_after_the_wait_timeout(runner, console, tmp_path):
    blueprint = tmp_path / "company.yaml"
    blueprint.write_text("company: {name: hello-saas}\n")
    console.answers[("GET", "/company")] = (
        200,
        dict(
            console.answers[("GET", "/company")][1],
            phase="activating",
            observedGeneration=0,
        ),
    )
    result = runner.invoke(
        cli.app, ["blueprint", "apply", str(blueprint), "--yes", "--wait-timeout", "0"]
    )
    assert result.exit_code == 1
    assert "still activating" in result.output and "company show" in result.output


def test_company_show_prints_the_crs_phase(runner, console):
    result = runner.invoke(cli.app, ["company", "show"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["phase"] == "converged"


def test_apply_previews_first_and_applies_with_yes(runner, console, tmp_path):
    blueprint = tmp_path / "company.yaml"
    blueprint.write_text("company: {name: hello-saas}\n")
    result = runner.invoke(
        cli.app, ["blueprint", "apply", str(blueprint), "--yes", "--no-wait"]
    )
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
    assert "poseidon login" in result.output and "--save" in result.output


def test_a_refused_saved_token_says_to_save_a_fresh_one(monkeypatch, config_home):
    monkeypatch.delenv("POSEIDON_TOKEN", raising=False)
    monkeypatch.setenv("POSEIDON_CONSOLE_URL", CONSOLE)
    path = config_home / "poseidon" / "token"
    path.parent.mkdir()
    path.write_text("stale\n")
    refused = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"detail": "token rejected"})
    )
    original = cli.ConsoleClient.__init__

    def scripted(self, base_url=None, token=None, transport=None, timeout=120.0):
        original(self, base_url, token, refused, timeout)

    monkeypatch.setattr(cli.ConsoleClient, "__init__", scripted)
    result = CliRunner().invoke(cli.app, ["whoami"])
    assert result.exit_code == 1
    assert "token rejected" in result.output and "--save" in result.output


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
    assert minted == [("human-owner", PLATFORM_NAMESPACE, ["poseidon-console"])]


def test_login_save_keeps_the_token_for_later_commands(monkeypatch, config_home):
    import types

    class FakeCore:
        def create_namespaced_service_account_token(self, name, namespace, body):
            return types.SimpleNamespace(
                status=types.SimpleNamespace(
                    token="tok-saved", expiration_timestamp="2026-09-18T00:00:00Z"
                )
            )

    import kubernetes

    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda: None)
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", FakeCore)
    monkeypatch.delenv("POSEIDON_TOKEN", raising=False)
    result = CliRunner().invoke(
        cli.app, ["login", "owner", "--save", "--duration", "604800"]
    )
    assert result.exit_code == 0, result.output
    path = config_home / "poseidon" / "token"
    assert "tok-saved" not in result.output and str(path) in result.output
    assert path.read_text() == "tok-saved\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ConsoleClient.from_env().token == "tok-saved"
