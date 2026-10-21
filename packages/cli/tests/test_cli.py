"""The CLI is a thin client: every command is one console call with the
token, its refusals are the console's, and nothing is decided here."""

import importlib.metadata
import json

import httpx
import pytest
from typer.testing import CliRunner

from common.audit import HEARTBEAT_DECIDE
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
            ("GET", "/healthz"): (200, {"ok": True, "version": "v0.1.0+2026.10.21"}),
            ("GET", "/whoami"): (
                200,
                {"human": "owner", "lifecycle": "human", "company": "hello-saas"},
            ),
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
    assert client.company()["phase"] == "converged"
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


def test_version_names_both_sides(runner, console, monkeypatch):
    """The CLI's own version is its package metadata; the console's is
    what the cluster runs. The health route is open, so no token is
    needed and none is required."""
    monkeypatch.delenv("POSEIDON_TOKEN")
    result = runner.invoke(cli.app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "cli": importlib.metadata.version("poseidon-cli"),
        "console": "v0.1.0+2026.10.21",
    }
    rendered = runner.invoke(cli.app, ["version"])
    assert "console v0.1.0+2026.10.21" in rendered.output


def test_version_without_a_console_still_names_the_cli(runner, console):
    console.answers[("GET", "/healthz")] = (503, {"detail": "down"})
    result = runner.invoke(cli.app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["console"] is None
    assert "console unreachable" in runner.invoke(cli.app, ["version"]).output


def test_verdict_prints_the_recorded_verdict(runner, console):
    result = runner.invoke(
        cli.app, ["verdict", "run-1", "merge", "approve", "--reason", "ok"]
    )
    assert result.exit_code == 0, result.output
    assert "verdict recorded: merge approved for run run-1, by owner" in result.output
    assert console.requests[-1][2] == {"approved": True, "reason": "ok"}


def test_a_verdict_is_approve_or_reject_on_a_known_gate(runner, console):
    """The words are the glossary's, and the choice is closed: no boolean
    flag, no gate the console does not have."""
    console.answers[("POST", "/runs/run-1/gates/budget")] = (
        200,
        {"run_id": "run-1", "gate": "budget", "approved": False, "principal": "owner"},
    )
    rejected = runner.invoke(
        cli.app, ["verdict", "run-1", "budget", "reject", "--reason", "no more spend"]
    )
    assert rejected.exit_code == 0, rejected.output
    assert console.requests[-1][:3] == (
        "POST",
        "/runs/run-1/gates/budget",
        {"approved": False, "reason": "no more spend"},
    )
    for args in (
        ["verdict", "run-1", "merge", "maybe", "--reason", "x"],
        ["verdict", "run-1", "door", "approve", "--reason", "x"],
        ["verdict", "run-1", "merge", "approve"],
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 2, (args, result.output)
    assert len(console.requests) == 1


def test_a_denied_verdict_exits_with_the_rule(runner):
    result = runner.invoke(
        cli.app, ["verdict", "run-2", "merge", "approve", "--reason", "ok"]
    )
    assert result.exit_code == 1
    assert "human_not_recognized" in result.output


def test_runs_lists_the_companys_runs(runner, console):
    result = runner.invoke(cli.app, ["runs", "--json"])
    assert result.exit_code == 0, result.output
    assert [r["run_id"] for r in json.loads(result.output)] == ["run-1", "run-0"]
    rendered = runner.invoke(cli.app, ["runs"])
    assert rendered.exit_code == 0, rendered.output
    assert "run-1" in rendered.output and "running" in rendered.output


def test_spend_is_the_companys_without_a_run_and_a_runs_with_one(runner, console):
    console.answers[("GET", "/spend")] = (200, {"usd": 3.1, "entries": 4})
    console.answers[("GET", "/runs/run-1/spend")] = (200, {"run_id": "run-1", "usd": 0.5})
    company = runner.invoke(cli.app, ["spend", "--json"])
    assert company.exit_code == 0 and json.loads(company.output)["usd"] == 3.1
    one = runner.invoke(cli.app, ["spend", "run-1", "--json"])
    assert one.exit_code == 0 and json.loads(one.output)["run_id"] == "run-1"
    assert "$3.10" in runner.invoke(cli.app, ["spend"]).output
    assert "$0.50" in runner.invoke(cli.app, ["spend", "run-1"]).output
    assert [p for _, p, _, _ in console.requests][:2] == ["/spend", "/runs/run-1/spend"]


def test_run_story_can_keep_only_the_decisions(runner, console):
    console.answers[("GET", "/runs/run-1/story")] = (200, [])
    runner.invoke(cli.app, ["run", "story", "run-1", "--decisions"])
    assert console.requests[-1][:2] == ("GET", "/runs/run-1/story")


def test_company_delete_names_the_running_runs_then_asks_the_console(runner, console):
    result = runner.invoke(cli.app, ["company", "delete", "--yes", "--reason", "done"])
    assert result.exit_code == 0, result.output
    assert "hello-saas: 1 running run(s)" in result.output and "run-1" in result.output
    assert "hello-saas deleted in 3 s; cancelled run-1" in result.output
    assert console.requests[-1][:3] == (
        "DELETE",
        "/company",
        {"reason": "done", "wait": True},
    )
    quick = runner.invoke(
        cli.app, ["company", "delete", "--yes", "--no-wait", "--reason", "done"]
    )
    assert quick.exit_code == 0, quick.output
    assert console.requests[-1][2] == {"reason": "done", "wait": False}


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


def test_company_show_prints_the_crs_phase_and_the_pause(runner, console):
    result = runner.invoke(cli.app, ["company", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["phase"] == "converged"
    rendered = runner.invoke(cli.app, ["company", "show"])
    assert rendered.exit_code == 0, rendered.output
    assert "converged" in rendered.output and "3 (observed 3)" in rendered.output
    assert "heartbeat" in rendered.output and "running" in rendered.output
    status, payload = console.answers[("GET", "/company")]
    console.answers[("GET", "/company")] = (
        status,
        {**payload, "paused": {"by": "owner", "at": "2026-01-01T00:00:00+00:00"}},
    )
    held = runner.invoke(cli.app, ["company", "show"])
    assert "paused by owner" in held.output, held.output


def test_pause_and_resume_carry_the_reason_and_wake_carries_none(runner, console):
    console.answers[("POST", "/heartbeat/pause")] = (
        200,
        {
            "company": "hello-saas",
            "paused": {"by": "owner", "at": "2026-01-01T00:00:00Z"},
        },
    )
    console.answers[("POST", "/heartbeat/resume")] = (
        200,
        {"company": "hello-saas", "paused": None},
    )
    console.answers[("POST", "/heartbeat/wake")] = (
        200,
        {"company": "hello-saas", "woken": True},
    )
    paused = runner.invoke(cli.app, ["pause", "--reason", "reading the ledger"])
    assert paused.exit_code == 0, paused.output
    assert "heartbeat paused by owner" in paused.output
    assert console.requests[-1][:3] == (
        "POST",
        "/heartbeat/pause",
        {"reason": "reading the ledger"},
    )
    resumed = runner.invoke(cli.app, ["resume", "--reason", "read it"])
    assert resumed.exit_code == 0, resumed.output
    assert "heartbeat resumed" in resumed.output
    assert console.requests[-1][:3] == (
        "POST",
        "/heartbeat/resume",
        {"reason": "read it"},
    )
    woken = runner.invoke(cli.app, ["heartbeat", "wake"])
    assert woken.exit_code == 0, woken.output
    assert "heartbeat woken" in woken.output
    assert console.requests[-1][:3] == ("POST", "/heartbeat/wake", None)
    # No reason, no act: the CLI refuses before the console hears.
    silent = runner.invoke(cli.app, ["pause"])
    assert silent.exit_code != 0
    assert console.requests[-1][:2] == ("POST", "/heartbeat/wake")


def test_whoami_is_two_lines_for_a_person(runner, console):
    result = runner.invoke(cli.app, ["whoami"])
    assert result.exit_code == 0, result.output
    assert "you are owner (human)" in result.output
    assert "company: hello-saas · converged" in result.output
    raw = runner.invoke(cli.app, ["whoami", "--json"])
    assert json.loads(raw.output)["company"] == "hello-saas"


def test_run_show_is_a_record_and_story_is_lines(runner, console):
    console.answers[("GET", "/runs/run-1")] = (
        200,
        {
            "run_id": "run-1",
            "task": "Implement #42",
            "status": "failed",
            "workflow": "implement",
            "pr_url": None,
            "verdict": None,
            "ticket_urls": [],
            "iterations": 2,
            "error": "verify never went green",
            "created_at": "2026-09-13T10:00:00+00:00",
            "finished_at": "2026-09-13T10:04:02+00:00",
            "company": "hello-saas",
            "execution": {
                "workflow_id": "implement-run-1",
                "status": "COMPLETED",
                "state": None,
            },
        },
    )
    console.answers[("GET", "/runs/run-1/story")] = (
        200,
        [
            {
                "ts": "2026-09-13T10:00:01+00:00",
                "run_id": "run-1",
                "agent_id": "pm-1",
                "role": "pm",
                "tool": HEARTBEAT_DECIDE,
                "decision": "allow",
                "rule": "decision_valid",
                "rationale": "the slice is next",
            }
        ],
    )
    shown = runner.invoke(cli.app, ["run", "show", "run-1"])
    assert shown.exit_code == 0, shown.output
    for expected in ("Implement #42", "failed", "4 min 02 s", "verify never went green"):
        assert expected in shown.output, shown.output
    story = runner.invoke(cli.app, ["run", "story", "run-1"])
    assert story.exit_code == 0, story.output
    assert "10:00:01" in story.output and HEARTBEAT_DECIDE in story.output
    assert "the slice is next" in story.output
    raw = runner.invoke(cli.app, ["run", "story", "run-1", "--json"])
    assert json.loads(raw.output)[0]["rationale"] == "the slice is next"


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
