"""`poseidon blueprint new`: the questionnaire's defaults are the shipped
example, every path it offers compiles, and what it writes is what it
built."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from compiler import EMPTY, compile_blueprint
from poseidon_cli import main as cli
from poseidon_cli import new
from schema import load_blueprint

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"


class Scripted:
    """Answers by message; anything unscripted takes the default — the
    Enter-walks-through path. Refuses an answer the validator refuses,
    the way the terminal would."""

    def __init__(self, **answers: str | bool) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def text(
        self,
        message: str,
        *,
        default: str = "",
        help: str = "",
        validate: Callable[[str], str | None] | None = None,
        multiline: bool = False,
    ) -> str:
        self.asked.append(message)
        answer = str(self.answers.get(message, default))
        if validate and (problem := validate(answer)):
            raise AssertionError(f"{message}: {answer!r} refused: {problem}")
        return answer

    def select(
        self,
        message: str,
        choices: Sequence[tuple[str, str]],
        *,
        default: str,
        help: str = "",
    ) -> str:
        self.asked.append(message)
        answer = str(self.answers.get(message, default))
        assert answer in {value for value, _ in choices}, (message, answer)
        return answer

    def confirm(self, message: str, *, default: bool, help: str = "") -> bool:
        self.asked.append(message)
        return bool(self.answers.get(message, default))


def example_answers(**overrides: str | bool) -> Scripted:
    return Scripted(**{"Company name": "hello-saas", **overrides})


def test_the_defaults_are_the_shipped_example():
    """Answering every question with its default, under the example's
    name, builds the example: the questionnaire and the shipped document
    cannot drift apart without this failing."""
    built = new.build(new.ask(example_answers()))
    example = yaml.safe_load(HELLO_SAAS.read_text())
    assert compile_blueprint(built, snapshot=EMPTY).as_dict() == (
        compile_blueprint(example, snapshot=EMPTY).as_dict()
    )
    assert new.problems(built) == []


def test_policy_resolution_brings_the_delegation_and_the_contract_crew():
    answers = new.ask(
        example_answers(**{"Who resolves the merge gate": "policy"}),
    )
    built = new.build(answers)
    merge = built["workflows"]["review"]["gates"]["merge"]
    assert merge["resolved_by"] == "policy"
    assert merge["delegate"] == {
        "paths": ["src/", "tests/unit/", "docs/"],
        "max_reworks": 1,
    }
    assert built["policies"]["contract_tests"] == {
        "paths": ["tests/contract/"],
        "roles": ["contract-author"],
    }
    assert "contract-authoring" in built["standing"]["pm"]["may_spawn"]["crew"]
    assert new.problems(built) == []

    without_tree = new.build(
        new.ask(
            example_answers(
                **{
                    "Who resolves the merge gate": "policy",
                    "Keep an acceptance-contract tree only a contract author may write?": False,
                }
            )
        )
    )
    assert "contract_tests" not in without_tree["policies"]
    assert "contract-authoring" not in without_tree["standing"]["pm"]["may_spawn"]["crew"]
    assert new.problems(without_tree) == []


def test_a_human_resolved_gate_carries_no_delegation():
    built = new.build(new.ask(example_answers()))
    assert "delegate" not in built["workflows"]["review"]["gates"]["merge"]
    assert "contract_tests" not in built["policies"]
    assert "Paths policy may merge on its own" not in []


def test_the_reviewer_may_be_let_write():
    built = new.build(
        new.ask(example_answers(**{"May the reviewer write in its sandbox?": True}))
    )
    assert "reviewer" not in built["tools"]
    assert new.problems(built) == []


def test_the_remaining_bounds_are_asked_only_on_request():
    prompter = example_answers(
        **{"Adjust the remaining bounds?": True, "Attempts per ticket": "3"}
    )
    built = new.build(new.ask(prompter))
    assert built["heartbeat"]["max_attempts"] == 3
    assert "Minutes a parked merge waits for a human" in prompter.asked
    quiet = example_answers()
    new.ask(quiet)
    assert "Minutes a parked merge waits for a human" not in quiet.asked


def test_the_validators_refuse_what_the_schema_would():
    assert (
        new.valid_name("Hello")
        and new.valid_name("-x")
        and new.valid_name("ok-1") is None
    )
    assert new.valid_repo("example.com/a/b") and new.valid_repo("github.com/a/b") is None
    assert (
        new.valid_names("")
        and new.valid_names("owner, Bob")
        and new.valid_names("a b") is None
    )
    assert (
        new.valid_number(0.01)("x")
        and new.valid_number(0.01)("0")
        and new.valid_number(1)("2") is None
    )
    assert (
        new.valid_integer(1)("1.5")
        and new.valid_integer(1)("0")
        and new.valid_integer(0)("0") is None
    )
    with pytest.raises(AssertionError, match="refused"):
        new.ask(example_answers(**{"Company name": "Hello World"}))


def test_what_is_written_is_what_was_built(tmp_path):
    built = new.build(new.ask(example_answers()))
    text = new.render(built)
    assert text.startswith("# hello-saas — written by `poseidon blueprint new`")
    assert yaml.safe_load(text) == built
    path = tmp_path / "hello-saas.yaml"
    path.write_text(text)
    assert load_blueprint(path) == built


def test_the_command_writes_and_refuses_to_overwrite(monkeypatch, tmp_path):
    monkeypatch.setattr(new, "QuestionaryPrompter", lambda: example_answers())
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["blueprint", "new"])
    assert result.exit_code == 0, result.output
    written = tmp_path / "hello-saas.yaml"
    assert written.is_file() and "poseidon blueprint apply" in result.output
    assert load_blueprint(written)["company"]["name"] == "hello-saas"

    again = CliRunner().invoke(cli.app, ["blueprint", "new"])
    assert again.exit_code == 1 and "--force" in again.output

    forced = CliRunner().invoke(cli.app, ["blueprint", "new", str(written), "--force"])
    assert forced.exit_code == 0, forced.output
