"""`poseidon blueprint new` — a blueprint written by answering questions.

A blueprint declares almost nothing by default on purpose: a defaulted
bound is authority nobody wrote down. Writing one by hand means copying
an example and knowing which fields are the company's and which are the
shape every company repeats. The questionnaire asks for the first —
the name, the vision, the humans, the repository, the gate, the money —
in the person's words, with the shipped example's answers as defaults
wherever a default is honest (the vision and the repository have none:
they are the two answers only the person can give), and writes the
second without asking. What it writes is coherent by construction (the
crews a workflow needs are spawnable, the stewardship crew is always
among them, a contract tree brings its author's crew, delegation only
under policy) and then compiled, the way the console would compile it,
before a byte lands on disk.

The prompts are one small protocol, so the questionnaire runs against a
terminal (questionary) or a script (the tests) alike.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import yaml

from compiler import EMPTY, compile_blueprint
from compiler.errors import CompileError
from schema import validate_blueprint
from schema.catalog import CREW_TEMPLATES, ROLES, STEWARDSHIP_CREW
from schema.documents import DOCUMENT_NAMES
from schema.tools import SANDBOX_WRITE_TOOLS
from schema.validate import ValidationError

API_VERSION = "poseidon.dev/v1"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REPO_PATTERN = re.compile(r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The examples' stand-in owner: a company pointed at it finds no
# repository, so the questionnaire refuses it where a person types.
PLACEHOLDER_OWNER = "example-org"

# The layout every company repeats, and the bounds the example ships with.
# A person changes them in the written file; the questionnaire asks only
# what is theirs to decide.
DOCUMENT_PATHS = {"strategy": "docs/STRATEGY.md", "architecture": "docs/ARCHITECTURE.md"}
SANDBOX_IMAGE = "poseidon-sandbox"
PM = "pm"
IMPLEMENT_CREW = "feature-delivery"
REVIEW_CREW = "feature-review"
CONTRACT_CREW = "contract-authoring"
CONTRACT_ROLE = "contract-author"
CONTRACT_PATHS = ["tests/contract/"]
EGRESS_PRESETS: dict[str, list[str]] = {
    "python": ["pypi.org", "files.pythonhosted.org"],
    "none": [],
}

# What the screen says before the first question, and above each group.
OPENING = (
    "A blueprint in about a dozen questions. Enter keeps the answer shown;"
    " Ctrl+C leaves nothing written."
)
SECTIONS = ("Company", "Repository", "Who decides", "Money", "Advanced")
# How a multi-line answer is finished — said on screen with the prompt,
# since Enter there adds a line.
MULTILINE_HINT = "Enter adds a line; Esc then Enter finishes"


class Prompter(Protocol):
    """What the questionnaire needs from whoever answers."""

    def say(self, text: str) -> None: ...

    def text(
        self,
        message: str,
        *,
        default: str = "",
        help: str = "",
        validate: Callable[[str], str | None] | None = None,
        multiline: bool = False,
    ) -> str: ...

    def select(
        self,
        message: str,
        choices: Sequence[tuple[str, str]],
        *,
        default: str,
        help: str = "",
    ) -> str: ...

    def confirm(self, message: str, *, default: bool, help: str = "") -> bool: ...


@dataclass
class Answers:
    """Everything the questionnaire asks, with the example's answers — the
    vision and the repository included, for `build()`; `ask()` offers
    neither as a default."""

    name: str = "my-company"
    vision: str = (
        "A small SaaS that turns titles into URL slugs, delivered and kept "
        "green by a company of agents that never exceeds its authority."
    )
    humans: list[str] = field(default_factory=lambda: ["owner"])
    repo_url: str = f"github.com/{PLACEHOLDER_OWNER}/hello-saas"
    base_ref: str = "main"
    test_command: str = "pytest -q"
    egress: list[str] = field(default_factory=lambda: list(EGRESS_PRESETS["python"]))
    reviewer_writes: bool = False
    resolver: str = "human"
    delegate_paths: list[str] = field(
        default_factory=lambda: ["src/", "tests/unit/", "docs/"]
    )
    max_reworks: int = 1
    contract_tree: bool = True
    per_run_usd: float = 5
    daily_cap_usd: float = 20
    max_concurrent_runs: int = 1
    # The remaining bounds, behind one "fine-tune?" question.
    max_concurrent: int = 2
    reconcile_minutes: int = 30
    max_attempts: int = 2
    max_open_tickets: int = 5
    budget_deadline_minutes: int = 60
    merge_deadline_minutes: int = 1440
    max_verify_retries: int = 2
    allowed_pattern: str = "^feature/[a-z0-9-]+$"
    deny_paths: list[str] = field(default_factory=lambda: [".github/workflows/"])


# -- validators: pure, so a terminal and a test refuse the same answers --------


def valid_name(value: str) -> str | None:
    if not NAME_PATTERN.match(value):
        return "lowercase letters, digits and hyphens, starting with a letter or digit"
    return None


def valid_vision(value: str) -> str | None:
    if not value.strip():
        return "a few sentences: what the company should build, and why"
    return None


def valid_repo(value: str) -> str | None:
    if not REPO_PATTERN.match(value):
        return "github.com/<owner>/<repository>"
    if value.split("/")[1] == PLACEHOLDER_OWNER:
        return f"{PLACEHOLDER_OWNER} is the examples' stand-in; name your repository"
    return None


def valid_names(value: str) -> str | None:
    names = _split(value)
    if not names:
        return "at least one name"
    bad = [n for n in names if valid_name(n)]
    return f"not a name: {', '.join(bad)}" if bad else None


def valid_paths(value: str) -> str | None:
    paths = _split(value)
    if not paths:
        return "at least one path"
    bad = [p for p in paths if p.startswith("/") or " " in p]
    return f"not a repository path: {', '.join(bad)}" if bad else None


def valid_number(minimum: float) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        try:
            number = float(value)
        except ValueError:
            return "a number"
        return None if number >= minimum else f"at least {minimum:g}"

    return check


def valid_integer(minimum: int) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        if not value.strip().lstrip("-").isdigit():
            return "a whole number"
        return None if int(value) >= minimum else f"at least {minimum}"

    return check


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", " ").split() if part.strip()]


# -- the questionnaire --------------------------------------------------------


def ask(prompter: Prompter, defaults: Answers | None = None) -> Answers:
    """Section by section, in the person's words: what the answer changes
    for them, with the charter's term once in the help where the file
    they get will show it. Every prompt but the vision and the repository
    is defaulted, so Enter walks through."""
    a = defaults or Answers()
    prompter.say(OPENING)

    prompter.say(SECTIONS[0])
    a.name = prompter.text(
        "Company name",
        default=a.name,
        help="short, lowercase, hyphens allowed (e.g. hello-saas); this names the "
        "company everywhere",
        validate=valid_name,
    )
    # Folded like the examples' `>-` scalars: line breaks typed in the
    # editor are spaces in the vision; a blank line stays a paragraph.
    a.vision = _fold(
        prompter.text(
            "What should this company build, and why?",
            help="a few sentences; the agents read this before every decision",
            validate=valid_vision,
            multiline=True,
        )
    )
    a.humans = _split(
        prompter.text(
            "Who may steer the company?",
            default=", ".join(a.humans),
            help="login names, comma-separated; they approve merges, raise budgets "
            "and can stop the company. On kind: owner, auditor",
            validate=valid_names,
        )
    )

    prompter.say(SECTIONS[1])
    a.repo_url = prompter.text(
        "GitHub repository",
        help="github.com/<owner>/<repo>; the company's code lives here",
        validate=valid_repo,
    )
    a.base_ref = prompter.text(
        "Main branch",
        default=a.base_ref,
        help="the branch the company delivers pull requests to; it stays protected",
    )
    a.test_command = prompter.text(
        "Test command",
        default=a.test_command,
        help="run from the repository root; a change counts as good only when this "
        "passes",
    )
    preset = prompter.select(
        "Internet access for the agents' workspace",
        [
            ("python", "Python packages only (pypi.org, files.pythonhosted.org)"),
            ("none", "None"),
            ("custom", "Hosts I will list"),
        ],
        default="python" if a.egress == EGRESS_PRESETS["python"] else "custom",
        help="everything not listed is blocked",
    )
    if preset == "custom":
        a.egress = _split(
            prompter.text(
                "Hosts",
                default=", ".join(a.egress),
                help="host names, comma-separated",
                validate=valid_paths,
            )
        )
    else:
        a.egress = list(EGRESS_PRESETS[preset])

    prompter.say(SECTIONS[2])
    a.reviewer_writes = (
        prompter.select(
            "Should the reviewer fix code, or only judge it?",
            [("judge", "Judge only (recommended)"), ("fix", "Judge and fix")],
            default="fix" if a.reviewer_writes else "judge",
        )
        == "fix"
    )
    a.resolver = prompter.select(
        "Who decides whether a pull request merges?",
        [
            ("human", "A person approves every merge"),
            ("policy", "Routine changes merge on their own; a person approves the rest"),
        ],
        default=a.resolver,
        help="you can change this later by editing the file",
    )
    if a.resolver == "policy":
        a.delegate_paths = _split(
            prompter.text(
                "Folders where a change may merge without a person",
                default=", ".join(a.delegate_paths),
                help="anything touching other files waits for approval",
                validate=valid_paths,
            )
        )
        a.max_reworks = int(
            prompter.text(
                "How many times may a rejected change be reworked before a person "
                "is asked?",
                default=str(a.max_reworks),
                validate=valid_integer(0),
            )
        )
        a.contract_tree = prompter.confirm(
            "Keep acceptance tests the code's author may not edit?",
            default=a.contract_tree,
            help=f"{CONTRACT_PATHS[0]} is written by a separate agent, so the code "
            "never grades itself",
        )

    prompter.say(SECTIONS[3])
    a.per_run_usd = float(
        prompter.text(
            "Spending limit per task, USD",
            default=_num(a.per_run_usd),
            help="a task stops here and waits for a person",
            validate=valid_number(0.01),
        )
    )
    a.daily_cap_usd = float(
        prompter.text(
            "Spending limit per day, USD",
            default=_num(max(a.daily_cap_usd, a.per_run_usd)),
            help="the company stops for the day here",
            validate=valid_number(a.per_run_usd),
        )
    )
    a.max_concurrent_runs = int(
        prompter.text(
            "How many tasks may the company work on at once?",
            default=str(a.max_concurrent_runs),
            validate=valid_integer(1),
        )
    )

    if prompter.confirm(
        "Fine-tune the advanced settings?",
        default=False,
        help="how often it checks in, retries, backlog size, waiting times, branch "
        "rules; the defaults are fine to start",
    ):
        prompter.say(SECTIONS[4])
        a.reconcile_minutes = _int(
            prompter,
            "Minutes between check-ins when nothing else happens",
            a.reconcile_minutes,
            1,
        )
        a.max_concurrent = _int(
            prompter,
            "Crews one task may have running at the same time",
            a.max_concurrent,
            1,
            help="an implement and its review",
        )
        a.max_attempts = _int(
            prompter, "Tries per ticket before it is given up", a.max_attempts, 1
        )
        a.max_open_tickets = _int(
            prompter,
            "Open tickets before the company stops planning new ones",
            a.max_open_tickets,
            1,
        )
        a.budget_deadline_minutes = _int(
            prompter,
            "Minutes a task waits at its spending limit for a person",
            a.budget_deadline_minutes,
            1,
        )
        a.merge_deadline_minutes = _int(
            prompter,
            "Minutes a pull request waits for a person's verdict",
            a.merge_deadline_minutes,
            1,
        )
        a.max_verify_retries = _int(
            prompter,
            "Times a task may re-run its tests before giving up",
            a.max_verify_retries,
            0,
        )
        a.allowed_pattern = prompter.text(
            "Pattern the company's branch names must match",
            default=a.allowed_pattern,
            help="a regular expression",
        )
        a.deny_paths = _split(
            prompter.text(
                "Files and folders the company may never change",
                default=", ".join(a.deny_paths),
                validate=valid_paths,
            )
        )
    return a


def _fold(text: str) -> str:
    paragraphs = [" ".join(p.split()) for p in text.strip().split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def _int(
    prompter: Prompter, message: str, current: int, minimum: int, help: str = ""
) -> int:
    return int(
        prompter.text(
            message, default=str(current), help=help, validate=valid_integer(minimum)
        )
    )


def _num(value: float) -> str:
    return f"{value:g}"


def instruction(help: str, multiline: bool = False) -> str | None:
    """What questionary shows beside a prompt: the help, and for a
    multi-line field how to finish — the one place that hint lives."""
    parts = [part for part in (help, MULTILINE_HINT if multiline else "") if part]
    return f"({'; '.join(parts)})" if parts else None


# -- the document -------------------------------------------------------------


def build(a: Answers) -> dict[str, Any]:
    """The blueprint the answers describe. Coherent by construction: the
    crews the workflows need are spawnable and the stewardship crew is
    always among them; a contract tree brings its author's crew; the
    delegation exists only under policy; the base branch is protected."""
    crews = {IMPLEMENT_CREW, REVIEW_CREW, STEWARDSHIP_CREW}
    policies: dict[str, Any] = {
        "branch_protection": {
            "protected": [a.base_ref],
            "allowed_pattern": a.allowed_pattern,
        },
        "deny_paths": list(a.deny_paths),
    }
    merge: dict[str, Any] = {
        "resolved_by": a.resolver,
        "deadline_minutes": a.merge_deadline_minutes,
    }
    if a.resolver == "policy":
        merge["delegate"] = {
            "paths": list(a.delegate_paths),
            "max_reworks": a.max_reworks,
        }
        if a.contract_tree:
            crews.add(CONTRACT_CREW)
            policies["contract_tests"] = {
                "paths": list(CONTRACT_PATHS),
                "roles": [CONTRACT_ROLE],
            }
    document: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "company": {
            "name": a.name,
            "vision": a.vision,
            "humans": list(a.humans),
            "documents": {name: DOCUMENT_PATHS[name] for name in DOCUMENT_NAMES},
        },
        "stack": {
            "repo": {"url": a.repo_url, "base_ref": a.base_ref},
            "toolchain": {
                "image": SANDBOX_IMAGE,
                "test_command": a.test_command,
                **({"egress_allow": list(a.egress)} if a.egress else {}),
            },
        },
        "standing": {
            PM: {
                "role": PM,
                "count": 1,
                "may_spawn": {
                    "crew": sorted(c for c in CREW_TEMPLATES if c in crews),
                    "max_concurrent": a.max_concurrent,
                },
            }
        },
        "tools": {},
        "workflows": {
            "implement": {
                "crew": IMPLEMENT_CREW,
                "max_verify_retries": a.max_verify_retries,
            },
            "review": {"crew": REVIEW_CREW, "gates": {"merge": merge}},
        },
        "policies": policies,
        "budgets": {
            "per_run": {
                "usd": _money(a.per_run_usd),
                "deadline_minutes": a.budget_deadline_minutes,
            }
        },
        "heartbeat": {
            "conductor": PM,
            "reconcile_minutes": a.reconcile_minutes,
            "max_concurrent_runs": a.max_concurrent_runs,
            "max_attempts": a.max_attempts,
            "max_open_tickets": a.max_open_tickets,
            "may_start": ["scope", "implement", "review"],
            "daily_cap": {"usd": _money(a.daily_cap_usd)},
        },
    }
    if not a.reviewer_writes:
        # The reviewer's catalog minus the writes: it judges, it does not fix.
        reviewer = [t for t in ROLES["reviewer"].tools if t not in SANDBOX_WRITE_TOOLS]
        document["tools"]["reviewer"] = {"allow": reviewer}
    return document


def _money(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


# The person's first reading of the file they own: one note per section,
# saying what it governs for them.
SECTION_NOTES = {
    "company": (
        "Your company: its name, what it should build and why (every agent\n"
        "reads this before every decision), who may steer it, and the two\n"
        "documents it writes for itself in its repository."
    ),
    "stack": "Where the code lives, and what the agents' workspace may reach and run.",
    "standing": (
        "The company's one permanent agent, the PM: it decides what to work\n"
        "on next, and which crews it may bring in for a task."
    ),
    "tools": (
        "What each role may do beyond reading. A role not listed keeps its\n"
        "full set; the reviewer here may judge code, not change it."
    ),
    "workflows": (
        "How a task is carried out and reviewed: which crew does each, how\n"
        "many test retries, and who approves a merge."
    ),
    "policies": "What the company may never do, whoever asks.",
    "budgets": "Spending limits: a task stops here and waits for a person.",
    "heartbeat": (
        "How the company keeps itself going: how often it checks in, how\n"
        "much it may run at once, how many tries and open tickets, and the\n"
        "day's spending limit."
    ),
}


def render(document: dict[str, Any]) -> str:
    """The document as a file: each section under a short note, in the
    order the reference reads them. Always valid YAML — the values are
    dumped, never templated."""
    parts = [
        f"# {document['company']['name']} — written by `poseidon blueprint new`.\n"
        "# Every value here is yours to change; `poseidon blueprint apply` makes\n"
        "# it the company's charter.\n",
        yaml.safe_dump({"apiVersion": document["apiVersion"]}, sort_keys=False),
    ]
    for section, note in SECTION_NOTES.items():
        if section not in document:
            continue
        commented = "\n".join(f"# {line}" for line in note.splitlines())
        body = yaml.safe_dump({section: document[section]}, sort_keys=False, width=88)
        parts.append(f"\n{commented}\n{body}")
    return "".join(parts)


def problems(document: dict[str, Any]) -> list[str]:
    """What the console would refuse — schema, catalog, gates, coherence."""
    try:
        validate_blueprint(document)
        compile_blueprint(document, snapshot=EMPTY)
    except (ValidationError, CompileError) as exc:
        return list(exc.problems)
    return []


# -- the terminal ---------------------------------------------------------------


class QuestionaryPrompter:
    """The questionnaire on a terminal: arrow-key choices, a multi-line
    editor for the vision with its submit hint on screen, a heading above
    each group of questions."""

    def say(self, text: str) -> None:
        import questionary

        style = "bold" if text in SECTIONS else ""
        questionary.print(("\n" if text in SECTIONS else "") + text, style=style)

    def text(
        self,
        message: str,
        *,
        default: str = "",
        help: str = "",
        validate: Callable[[str], str | None] | None = None,
        multiline: bool = False,
    ) -> str:
        import questionary

        def ok(value: str) -> bool | str:
            problem = validate(value) if validate else None
            return True if problem is None else problem

        answer = questionary.text(
            message,
            default=default,
            instruction=instruction(help, multiline),
            validate=ok,
            multiline=multiline,
        ).unsafe_ask()
        return answer if answer is not None else default

    def select(
        self,
        message: str,
        choices: Sequence[tuple[str, str]],
        *,
        default: str,
        help: str = "",
    ) -> str:
        import questionary

        options = [
            questionary.Choice(title=label, value=value) for value, label in choices
        ]
        answer = questionary.select(
            message,
            choices=options,
            default=next((o for o in options if o.value == default), None),
            instruction=instruction(help),
        ).unsafe_ask()
        return answer if answer is not None else default

    def confirm(self, message: str, *, default: bool, help: str = "") -> bool:
        import questionary

        answer = questionary.confirm(
            message, default=default, instruction=instruction(help)
        ).unsafe_ask()
        return default if answer is None else bool(answer)
