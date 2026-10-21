"""`poseidon blueprint new`: the questionnaire's defaults are the shipped
example, the two answers only a person can give have none, every path it
offers compiles, and what it writes is what it built."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from blueprint import load_blueprint
from compiler import EMPTY, compile_blueprint
from poseidon_cli import main as cli
from poseidon_cli import new

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"

VISION = "What should this company build, and why?"
REPOSITORY = "GitHub repository"
MERGE = "Who decides whether a pull request merges?"
REVIEWER = "Should the reviewer fix code, or only judge it?"
CONTRACT = "Keep acceptance tests the code's author may not edit?"
ADVANCED = "Fine-tune the advanced settings?"
# A repository of the person's own: the example's owner is a placeholder
# the questionnaire refuses.
OWN_REPO = "github.com/acme/hello-saas"


class Scripted:
    """Answers by message; anything unscripted takes the default — the
    Enter-walks-through path. Refuses an answer the validator refuses,
    the way the terminal would, and keeps what was said and asked."""

    def __init__(self, **answers: str | bool) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

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
    """The example's name, its vision and a repository of one's own — the
    three answers a person types — with every other default."""
    return Scripted(
        **{
            "Company name": "hello-saas",
            VISION: new.Answers().vision,
            REPOSITORY: OWN_REPO,
            **overrides,
        }
    )


def test_the_defaults_are_the_shipped_example():
    """Answering every question with its default, under the example's
    name, vision and a repository, builds the example: the questionnaire
    and the shipped document cannot drift apart without this failing."""
    built = new.build(new.ask(example_answers()))
    example = yaml.safe_load(HELLO_SAAS.read_text())
    example["stack"]["repo"]["url"] = OWN_REPO  # the one answer the example fakes
    assert compile_blueprint(built, snapshot=EMPTY).as_dict() == (
        compile_blueprint(example, snapshot=EMPTY).as_dict()
    )
    assert new.problems(built) == []


def test_the_vision_and_the_repository_have_no_default():
    """The two answers only the person can give: an empty vision and the
    examples' placeholder owner are refused where a person types."""
    with pytest.raises(AssertionError, match="refused"):
        new.ask(Scripted(**{"Company name": "x", REPOSITORY: OWN_REPO}))
    with pytest.raises(AssertionError, match="stand-in"):
        new.ask(
            Scripted(
                **{
                    "Company name": "x",
                    VISION: "Build a thing.",
                    REPOSITORY: "github.com/example-org/hello-saas",
                }
            )
        )
    assert new.valid_vision("  \n") and new.valid_vision("Build a thing.") is None


def test_the_screen_opens_with_the_rules_and_names_each_section():
    prompter = example_answers(**{ADVANCED: True})
    new.ask(prompter)
    assert prompter.said[0] == new.OPENING
    assert prompter.said[1:] == list(new.SECTIONS)
    assert "Advanced" not in example_answers().said


def test_the_multiline_hint_is_on_screen_with_the_prompt():
    """questionary's own hint would be overwritten by the help; the
    instruction composes both, so the one prompt where Enter does not
    submit says how to."""
    assert new.instruction("a few sentences", multiline=True) == (
        "(a few sentences; Enter adds a line; Esc then Enter finishes)"
    )
    assert (
        new.instruction("", multiline=True)
        == "(Enter adds a line; Esc then Enter finishes)"
    )
    assert new.instruction("short") == "(short)" and new.instruction("") is None


def test_policy_resolution_brings_the_delegation_and_the_contract_crew():
    answers = new.ask(example_answers(**{MERGE: "policy"}))
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
        new.ask(example_answers(**{MERGE: "policy", CONTRACT: False}))
    )
    assert "contract_tests" not in without_tree["policies"]
    assert "contract-authoring" not in without_tree["standing"]["pm"]["may_spawn"]["crew"]
    assert new.problems(without_tree) == []


def test_a_human_resolved_gate_carries_no_delegation():
    prompter = example_answers()
    built = new.build(new.ask(prompter))
    assert "delegate" not in built["workflows"]["review"]["gates"]["merge"]
    assert "contract_tests" not in built["policies"]
    assert "Folders where a change may merge without a person" not in prompter.asked


def test_the_reviewer_may_be_let_fix():
    built = new.build(new.ask(example_answers(**{REVIEWER: "fix"})))
    assert "reviewer" not in built["tools"]
    assert new.problems(built) == []


def test_concurrency_is_one_question_in_the_main_flow():
    """A person is asked how many tasks may run at once; the PM's crew
    cap, a platform knob no newcomer can tell apart from it, waits under
    the advanced settings with a help that says which is which."""
    quiet = example_answers(**{"How many tasks may the company work on at once?": "3"})
    built = new.build(new.ask(quiet))
    assert built["heartbeat"]["max_concurrent_runs"] == 3
    assert built["standing"]["pm"]["may_spawn"]["max_concurrent"] == 2
    assert (
        "Crews the company may have running at the same time, across its tasks"
        not in quiet.asked
    )
    tuned = example_answers(
        **{
            ADVANCED: True,
            "Crews the company may have running at the same time, across its tasks": "4",
        }
    )
    assert new.build(new.ask(tuned))["standing"]["pm"]["may_spawn"]["max_concurrent"] == 4


def test_the_advanced_settings_are_asked_only_on_request():
    prompter = example_answers(
        **{ADVANCED: True, "Tries per ticket before it is given up": "3"}
    )
    built = new.build(new.ask(prompter))
    assert built["heartbeat"]["max_attempts"] == 3
    assert "Minutes a pull request waits for a person's verdict" in prompter.asked
    quiet = example_answers()
    new.ask(quiet)
    assert "Minutes a pull request waits for a person's verdict" not in quiet.asked


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
    # Each section opens with its note for the person, not the platform.
    assert "# Spending limits: a task stops here and waits for a person." in text
    assert "Rego" not in text and "procedure" not in text
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
