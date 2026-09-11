"""`poseidon blueprint new` — a blueprint written by answering questions.

A blueprint declares almost nothing by default on purpose: a defaulted
bound is authority nobody wrote down. Writing one by hand means copying
an example and knowing which fields are the company's and which are the
shape every company repeats. The questionnaire asks for the first —
the name, the vision, the humans, the repository, the gate, the money —
with the shipped example's answers as defaults, and writes the second
without asking. What it writes is coherent by construction (the crews a
workflow needs are spawnable, the stewardship crew is always among them,
a contract tree brings its author's crew, delegation only under policy)
and then compiled, the way the console would compile it, before a byte
lands on disk.

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


class Prompter(Protocol):
    """What the questionnaire needs from whoever answers."""

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
    """Everything the questionnaire asks, with the example's answers."""

    name: str = "my-company"
    vision: str = (
        "A small SaaS that turns titles into URL slugs, delivered and kept "
        "green by a company of agents that never exceeds its authority."
    )
    humans: list[str] = field(default_factory=lambda: ["owner"])
    repo_url: str = "github.com/example-org/hello-saas"
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
    max_concurrent: int = 2
    # The remaining bounds, behind one "adjust them?" question.
    reconcile_minutes: int = 30
    max_concurrent_runs: int = 1
    max_attempts: int = 2
    max_open_tickets: int = 5
    pause_minutes: int = 60
    deadline_minutes: int = 1440
    max_verify_retries: int = 2
    allowed_pattern: str = "^feature/[a-z0-9-]+$"
    deny_paths: list[str] = field(default_factory=lambda: [".github/workflows/"])


# -- validators: pure, so a terminal and a test refuse the same answers --------


def valid_name(value: str) -> str | None:
    if not NAME_PATTERN.match(value):
        return "lowercase letters, digits and hyphens, starting with a letter or digit"
    return None


def valid_repo(value: str) -> str | None:
    if not REPO_PATTERN.match(value):
        return "github.com/<owner>/<repository>"
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
    """Section by section, every prompt defaulted so Enter walks through."""
    a = defaults or Answers()

    # company
    a.name = prompter.text(
        "Company name",
        default=a.name,
        help="Becomes the Rego package, the workflow ids and every registry row's key",
        validate=valid_name,
    )
    # Folded like the examples' `>-` scalars: line breaks typed in the
    # editor are spaces in the vision; a blank line stays a paragraph.
    a.vision = _fold(
        prompter.text(
            "Vision",
            default=a.vision,
            help="The one text a human writes: it reaches every procedure but deliver",
            multiline=True,
        )
    )
    a.humans = _split(
        prompter.text(
            "Humans who may act on the company",
            default=", ".join(a.humans),
            help="Answer its gates, start or cancel a run, wake the heartbeat, apply, delete",
            validate=valid_names,
        )
    )

    # stack
    a.repo_url = prompter.text(
        "Repository",
        default=a.repo_url,
        help="The one repository the company works on",
        validate=valid_repo,
    )
    a.base_ref = prompter.text(
        "Base branch",
        default=a.base_ref,
        help="Changes are measured and delivered against it",
    )
    a.test_command = prompter.text(
        "Test command",
        default=a.test_command,
        help="What a sandbox runs to know it is green",
    )
    preset = prompter.select(
        "Egress the sandbox may reach",
        [
            ("python", "Python: pypi.org and files.pythonhosted.org"),
            ("none", "Nothing"),
            ("custom", "Hosts I will list"),
        ],
        default="python" if a.egress == EGRESS_PRESETS["python"] else "custom",
        help="The entire allowlist; everything else is refused at the egress proxy",
    )
    if preset == "custom":
        a.egress = _split(
            prompter.text("Hosts", default=", ".join(a.egress), validate=valid_paths)
        )
    else:
        a.egress = list(EGRESS_PRESETS[preset])

    # who may write
    a.reviewer_writes = prompter.confirm(
        "May the reviewer write in its sandbox?",
        default=a.reviewer_writes,
        help="No: it judges, it does not fix — the example's choice",
    )

    # the merge gate
    a.resolver = prompter.select(
        "Who resolves the merge gate",
        [
            ("human", "A human answers every merge"),
            ("policy", "Policy merges passing in-class work; a human keeps the rest"),
        ],
        default=a.resolver,
        help="The one authority a company can be handed, and it is a diff to take back",
    )
    if a.resolver == "policy":
        a.delegate_paths = _split(
            prompter.text(
                "Paths policy may merge on its own",
                default=", ".join(a.delegate_paths),
                help="Prefixes; anything outside them parks on a human",
                validate=valid_paths,
            )
        )
        a.max_reworks = int(
            prompter.text(
                "Reworks before a failed review parks on a human",
                default=str(a.max_reworks),
                validate=valid_integer(0),
            )
        )
        a.contract_tree = prompter.confirm(
            "Keep an acceptance-contract tree only a contract author may write?",
            default=a.contract_tree,
            help=f"{CONTRACT_PATHS[0]}, graded by different hands than the code",
        )

    # money and concurrency
    a.per_run_usd = float(
        prompter.text(
            "Per-run cap, USD", default=_num(a.per_run_usd), validate=valid_number(0.01)
        )
    )
    a.daily_cap_usd = float(
        prompter.text(
            "Daily cap, USD",
            default=_num(max(a.daily_cap_usd, a.per_run_usd)),
            help="The day's hard stop; at least one run's worth",
            validate=valid_number(a.per_run_usd),
        )
    )
    a.max_concurrent = int(
        prompter.text(
            "Crews the PM may run at once",
            default=str(a.max_concurrent),
            validate=valid_integer(1),
        )
    )

    if prompter.confirm(
        "Adjust the remaining bounds?",
        default=False,
        help="Heartbeat cadence and caps, the budget pause, the gate deadline, rework and branch rules",
    ):
        a.reconcile_minutes = _int(
            prompter, "Reconcile every N minutes", a.reconcile_minutes, 1
        )
        a.max_concurrent_runs = _int(
            prompter,
            "Runs the heartbeat may have going at once",
            a.max_concurrent_runs,
            1,
        )
        a.max_attempts = _int(prompter, "Attempts per ticket", a.max_attempts, 1)
        a.max_open_tickets = _int(
            prompter, "Open tickets before scoping stops", a.max_open_tickets, 1
        )
        a.pause_minutes = _int(
            prompter,
            "Minutes a run waits at its cap for someone to raise it",
            a.pause_minutes,
            1,
        )
        a.deadline_minutes = _int(
            prompter, "Minutes a parked merge waits for a human", a.deadline_minutes, 1
        )
        a.max_verify_retries = _int(
            prompter, "Verify retries per implement run", a.max_verify_retries, 0
        )
        a.allowed_pattern = prompter.text(
            "Branch pattern the company may push", default=a.allowed_pattern
        )
        a.deny_paths = _split(
            prompter.text(
                "Paths no role may touch",
                default=", ".join(a.deny_paths),
                validate=valid_paths,
            )
        )
    return a


def _fold(text: str) -> str:
    paragraphs = [" ".join(p.split()) for p in text.strip().split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def _int(prompter: Prompter, message: str, current: int, minimum: int) -> int:
    return int(
        prompter.text(message, default=str(current), validate=valid_integer(minimum))
    )


def _num(value: float) -> str:
    return f"{value:g}"


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
        "deadline_minutes": a.deadline_minutes,
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
            "per_run": {"usd": _money(a.per_run_usd), "pause_minutes": a.pause_minutes}
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


SECTION_NOTES = {
    "company": (
        "The company: its name, the vision every procedure reads, the people\n"
        "who may act on it through the console, and the two documents it\n"
        "writes for itself in its repository."
    ),
    "stack": "The one repository the company works on, and the sandbox it works in.",
    "standing": (
        "The standing PM: decides the next action on every heartbeat tick and\n"
        "is the authority every crew is fulfilled under."
    ),
    "tools": (
        "Where a role's catalog is narrowed. A role not listed keeps its\n"
        "catalog tools; the reviewer here drops the writes — it judges."
    ),
    "workflows": (
        "The two crewed workflows: the crew that fulfils each, its bounds,\n"
        "and the gate a review parks on."
    ),
    "policies": "What no run may do, whoever asks.",
    "budgets": "Spend is a stop, not a warning.",
    "heartbeat": (
        "The company's own initiative: who decides, what it may start, how\n"
        "many at once, how many tries, how large the backlog, the day's cap."
    ),
}


def render(document: dict[str, Any]) -> str:
    """The document as a file: each section under a short note, in the
    order the reference reads them. Always valid YAML — the values are
    dumped, never templated."""
    parts = [
        f"# {document['company']['name']} — written by `poseidon blueprint new`;\n"
        "# every value here is yours to change, then `poseidon blueprint apply`.\n",
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
    editor for the vision (Esc then Enter, or Alt+Enter, submits)."""

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
            instruction=f"({help})" if help else None,
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
            instruction=f"({help})" if help else None,
        ).unsafe_ask()
        return answer if answer is not None else default

    def confirm(self, message: str, *, default: bool, help: str = "") -> bool:
        import questionary

        answer = questionary.confirm(
            message, default=default, instruction=f"({help})" if help else None
        ).unsafe_ask()
        return default if answer is None else bool(answer)
