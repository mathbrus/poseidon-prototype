"""Render the reference pages' tables from the code they describe.

mkdocs-gen-files runs this at build time (see mkdocs.yml). A page under
docs/ carries a line `<!-- gen: NAME -->` wherever a table belongs; this
script replaces the marker with the table rendered from the catalogs, the
gateway's tool definitions, the `poseidon` CLI, the console's routes, the
audit event registry, the Temporal workflows, the blueprint JSON Schema
the bounds an example compiles to, the policy rule registry and the
environment file, and re-emits the page. Nothing rendered here is
committed, so nothing here can drift from the code. A marker naming no
generator, a generator no page uses, a tool without a docstring or a
blueprint field without a description fails the build.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mkdocs_gen_files
import typer.main
import yaml

from agent_host.models import MODEL_FAMILIES
from blueprint import tools
from blueprint.bounds import render_bounds
from blueprint.catalog import CREW_TEMPLATES, PROCEDURES, ROLES
from blueprint.schema import BLUEPRINT_SCHEMA
from common.audit import PLATFORM_EVENTS
from common.console import ROUTES
from common.policy import GATE_RESOLVE, RULES, RUN_START, rego_slug
from common.workflows import WORKFLOWS, workflow_id
from compiler import compile_blueprint
from console.crs import company_cr
from orchestrator import workflows
from poseidon_cli.main import app as cli_app

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
GATEWAY_APP = REPO / "packages/mcp-gateway/src/mcp_gateway/app.py"
ENV_EXAMPLE = REPO / "deploy/kind/.env.example"
CHARTER = REPO / "CLAUDE.md"

MARKER = re.compile(r"<!-- gen: ([\w.-]+)(?::([\w.-]+))? -->")


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return "\n".join(lines)


def code(value: object) -> str:
    return f"`{value}`"


def codes(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(code(v) for v in values) or "—"


# -- roles, crews, procedures ----------------------------------------------


def roles(_: str | None) -> str:
    rows = [
        [code(r.name), r.summary, codes(r.procedures), codes(r.may_spawn), codes(r.tools)]
        for r in ROLES.values()
    ]
    return table(["Role", "What it is", "Procedures", "May spawn", "Tools"], rows)


def crews(_: str | None) -> str:
    rows = [[code(c.name), c.summary, codes(c.roles)] for c in CREW_TEMPLATES.values()]
    return table(["Crew", "What it is", "Roles"], rows)


def procedures(_: str | None) -> str:
    rows = [
        [code(p), codes([r.name for r in ROLES.values() if p in r.procedures])]
        for p in PROCEDURES
    ]
    return table(["Procedure", "Run by"], rows)


# -- the gateway's tools ----------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Tool:
    name: str
    arguments: str
    description: str


def _tool_definitions() -> dict[str, Tool]:
    """Every `@mcp.tool(name=...)` in the gateway, read from the source so
    no service has to be constructed to list them."""
    found: dict[str, Tool] = {}
    for node in ast.walk(ast.parse(GATEWAY_APP.read_text())):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "tool"
            ):
                continue
            named = next(kw.value for kw in deco.keywords if kw.arg == "name")
            # A literal, or a constant of the tool catalog by name.
            name = (
                named.value
                if isinstance(named, ast.Constant)
                else getattr(tools, named.id)  # type: ignore[attr-defined]
            )
            description = ast.get_docstring(node)
            if not description:
                raise SystemExit(f"gen_docs: tool {name!r} has no docstring")
            found[name] = Tool(
                name=name,
                arguments=_arguments(node.args),
                description=" ".join(description.split()),
            )
    return found


def _arguments(args: ast.arguments) -> str:
    """`run_id, path, view_range?` — required first, optional marked, a
    literal default shown; `ctx` is the server's, not the caller's."""
    positional = [a.arg for a in args.args]
    defaults: dict[str, ast.expr | None] = {}
    if args.defaults:
        defaults.update(
            zip(positional[-len(args.defaults) :], args.defaults, strict=True)
        )
    defaults.update(zip([a.arg for a in args.kwonlyargs], args.kw_defaults, strict=True))
    rendered = []
    for name in positional + [a.arg for a in args.kwonlyargs]:
        if name == "ctx":
            continue
        default = defaults.get(name)
        if name not in defaults:
            rendered.append(name)
        elif default is None or (
            isinstance(default, ast.Constant) and default.value is None
        ):
            rendered.append(name + "?")
        else:
            rendered.append(f"{name}={ast.unparse(default)}")
    return ", ".join(rendered)


TOOL_GROUPS: dict[str, list[str]] = {
    "sandbox": tools.SANDBOX_TOOLS,
    "repository": tools.GIT_TOOLS
    + tools.REVIEW_TOOLS
    + [t for t in tools.PLATFORM_TOOLS if t != tools.ISSUES_CLOSE],
    "issues": tools.ISSUE_TOOLS + [tools.ISSUES_CLOSE],
    "architecture": tools.ARCHITECTURE_TOOLS,
}


def tool_table(group: str | None) -> str:
    if group not in TOOL_GROUPS:
        raise SystemExit(f"gen_docs: unknown tool group {group!r}")
    defined = _tool_definitions()
    if set(defined) != set(tools.KNOWN_TOOLS):
        raise SystemExit(
            "gen_docs: the gateway's tools and blueprint.tools.KNOWN_TOOLS differ: "
            f"{sorted(set(defined) ^ set(tools.KNOWN_TOOLS))}"
        )
    rows = []
    for name in TOOL_GROUPS[group]:
        t = defined[name]
        holders = [r.name for r in ROLES.values() if name in r.tools]
        rows.append(
            [
                code(t.name),
                code(t.arguments),
                first_sentence(t.description),
                codes(holders) or "—",
            ]
        )
    return table(["Tool", "Arguments", "What it does", "Catalog roles holding it"], rows)


def first_sentence(text: str) -> str:
    """The docstring's first sentence. The whole docstring is the tool
    description the agents read; the reference shows its summary."""
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return match.group(1) if match else text


def tool_count(_: str | None) -> str:
    return str(len(tools.KNOWN_TOOLS))


# -- the CLI ----------------------------------------------------------------


def _param_row(param: Any) -> list[str]:
    if param.param_type_name == "argument":
        name = code(f"<{param.name}>")
    else:
        name = ", ".join(code(o) for o in param.opts)
        if not param.is_flag:
            name += " " + code(f"<{param.type.name}>")
    default = param.default
    shown = (
        "required"
        if param.required
        else ("—" if default in (None, False, "") else code(default))
    )
    return [name, shown, " ".join((getattr(param, "help", "") or "").split())]


def _examples(cmd: Any) -> str:
    lines = [line.strip() for line in (cmd.epilog or "").splitlines()]
    lines = [line for line in lines if line and line != "Examples:"]
    if not lines:
        raise SystemExit(f"gen_docs: command {cmd.name!r} has no examples")
    return "```bash\n" + "\n".join(lines) + "\n```"


def cli(_: str | None) -> str:
    """One entry per command: what it does, its synopsis, its arguments
    and options, and its examples — all from the command itself."""
    out: list[str] = []

    def walk(cmd: Any, prog: str) -> None:
        subcommands = getattr(cmd, "commands", {})
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, f"{prog} {name}")
            return
        ctx = cmd.context_class(cmd, info_name=prog)
        usage = " ".join([prog, *cmd.collect_usage_pieces(ctx)])
        rows = [_param_row(p) for p in cmd.params if p.name != "help"]
        entry = [
            f"### `{prog}`",
            " ".join((cmd.help or "").split()),
            f"```text\n{usage}\n```",
        ]
        if rows:
            entry.append(table(["Argument or option", "Default", "Description"], rows))
        entry.append(_examples(cmd))
        out.append("\n\n".join(entry))

    walk(typer.main.get_command(cli_app), "poseidon")
    return "\n\n".join(out)


# -- the console -------------------------------------------------------------


def console_routes(_: str | None) -> str:
    """The console's route table, as the console interface declares it."""
    rows = [
        [code(f"{r.method} {r.path}"), r.summary, code(r.decision) if r.decision else "—"]
        for r in ROUTES
    ]
    return table(["Route", "What it does", "Decision"], rows)


def console_acts(_: str | None) -> str:
    """The console's acts: every platform event the console writes, with
    the decision it must win and the rule an allow is recorded under —
    `rego_slug` of the act's name, as the compiler emits it; the two acts
    whose rule also names what was granted are spelled with their
    placeholders."""
    rules = {
        GATE_RESOLVE: "`gate_resolve_<gate>_approve` / `_reject`",
        RUN_START: "`run_start_<workflow>`",
    }
    rows = [
        [
            e.meaning,
            code(e.name),
            rules.get(e.name) or code(rego_slug(e.name)),
            "yes" if e.rationale else "—",
        ]
        for e in PLATFORM_EVENTS
        if e.source == "console"
    ]
    return table(["Act", "Decision", "Rule on allow", "Owes a rationale"], rows)


def audit_events(_: str | None) -> str:
    """The platform's own event names, from the registry every writer and
    reader spells them from."""
    rows = [
        [code(e.name), code(e.source), e.meaning, "yes" if e.rationale else "—"]
        for e in PLATFORM_EVENTS
    ]
    return table(["Event", "Source", "What the row records", "Owes a rationale"], rows)


# -- the Temporal contract ---------------------------------------------------

_WORKFLOW_CLASSES: dict[str, tuple[type, type]] = {
    "scope": (workflows.ScopeWorkflow, workflows.ScopeInput),
    "implement": (workflows.ImplementWorkflow, workflows.ImplementInput),
    "review": (workflows.ReviewWorkflow, workflows.ReviewInput),
    "heartbeat": (workflows.HeartbeatWorkflow, workflows.HeartbeatInput),
}


def models(_: str | None) -> str:
    rows = [
        [
            family.name,
            codes(family.examples),
            "Responses" if family.api_mode == "responses" else "chat completions",
            "yes" if family.reasoning_effort else "no",
        ]
        for family in MODEL_FAMILIES
    ]
    return table(["Family", "Names, for example", "Wire shape", "Reasoning effort"], rows)


def _handlers(cls: type, kind: str) -> list[tuple[str, str, str]]:
    """(name, arguments, first sentence of the docstring) for every
    `@workflow.signal` / `@workflow.query` on the class."""
    found = []
    for attr in vars(cls).values():
        definition = getattr(attr, f"__temporal_{kind}_definition", None)
        if definition is None:
            continue
        params = list(inspect.signature(attr).parameters)[1:]
        doc = " ".join((attr.__doc__ or "").split())
        found.append((definition.name, ", ".join(params), doc))
    return found


def workflow_table(_: str | None) -> str:
    if set(_WORKFLOW_CLASSES) != set(WORKFLOWS):
        raise SystemExit("gen_docs: WORKFLOWS and the workflow classes differ")
    rows = []
    for name in WORKFLOWS:
        cls, input_cls = _WORKFLOW_CLASSES[name]
        key = "{company}" if name == "heartbeat" else "{run_id}"
        fields = [
            f.name if f.default is dataclasses.MISSING else f"{f.name}={f.default!r}"
            for f in dataclasses.fields(input_cls)
        ]
        signals = codes([s[0] for s in _handlers(cls, "signal")])
        queries = codes([q[0] for q in _handlers(cls, "query")])
        rows.append(
            [
                code(name),
                code(workflow_id(name, key)),
                code(f"{input_cls.__name__}({', '.join(fields)})"),
                signals,
                queries,
            ]
        )
    return table(["Workflow", "Workflow id", "Input", "Signals", "Queries"], rows)


def signal_table(_: str | None) -> str:
    rows = []
    for name in WORKFLOWS:
        cls, _input = _WORKFLOW_CLASSES[name]
        for signal, signature, doc in _handlers(cls, "signal"):
            rows.append(
                [code(name), code(signal), code(signature) if signature else "—", doc]
            )
    return table(["Workflow", "Signal", "Arguments", "What it resolves"], rows)


# -- the blueprint schema ----------------------------------------------------


def _type_of(node: dict[str, Any]) -> str:
    kind = node.get("type", "")
    if kind == "array":
        return f"array of {_type_of(node['items'])}"
    if kind == "object" and isinstance(node.get("additionalProperties"), dict):
        return "map of " + _type_of(node["additionalProperties"])
    return kind


def _constraints(node: dict[str, Any]) -> str:
    parts = []
    target = node["items"] if node.get("type") == "array" else node
    if "enum" in target:
        parts.append("one of " + codes([str(v) for v in target["enum"]]))
    if "pattern" in target:
        parts.append("matches " + code(target["pattern"]))
    for key, label in (
        ("minimum", "≥ {}"),
        ("exclusiveMinimum", "> {}"),
        ("maxLength", "at most {} characters"),
        ("minItems", "at least {} item(s)"),
        ("minProperties", "at least {} entry"),
    ):
        if key in node:
            parts.append(label.format(node[key]))
    if node.get("minLength") == 1 and "pattern" not in node:
        parts.append("non-empty")
    if node.get("uniqueItems"):
        parts.append("unique")
    return "; ".join(parts)


def _description(node: dict[str, Any], path: str) -> str:
    if "description" not in node:
        raise SystemExit(f"gen_docs: blueprint field {path} has no description")
    return node["description"]


def _rows(node: dict[str, Any], path: str, required: bool) -> list[list[str]]:
    kind = _type_of(node)
    constraints = _constraints(node)
    rows = [
        [
            code(path),
            kind + (f" ({constraints})" if constraints else ""),
            "yes" if required else "",
            _description(node, path),
        ]
    ]
    if node.get("type") != "object":
        return rows
    for name, child in node.get("properties", {}).items():
        rows += _rows(child, f"{path}.{name}", name in node.get("required", []))
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        rows += _rows(extra, f"{path}.<name>", False)
    return rows


def blueprint_fields(section: str | None) -> str:
    top = BLUEPRINT_SCHEMA["properties"]
    if section not in top:
        raise SystemExit(f"gen_docs: unknown blueprint section {section!r}")
    rows = _rows(top[section], section, section in BLUEPRINT_SCHEMA["required"])
    return table(["Field", "Type", "Required", "Description"], rows)


def blueprint_sections(_: str | None) -> str:
    top = BLUEPRINT_SCHEMA["properties"]
    rows = [
        [
            f"[`{name}`](#{name.lower()})",
            "yes" if name in BLUEPRINT_SCHEMA["required"] else "",
            _description(node, name),
        ]
        for name, node in top.items()
    ]
    return table(["Section", "Required", "What it declares"], rows)


# -- the policy rules --------------------------------------------------------


def policy_rules(_: str | None) -> str:
    rows = [[code(r.name), r.effect, r.decides] for r in RULES]
    return table(["Rule", "Effect", "What it decides"], rows)


# -- the environment ---------------------------------------------------------

ENV_VAR = re.compile(r"^#?([A-Z][A-Z0-9_]*)=(.*)$")


def environment(_: str | None) -> str:
    """deploy/kind/.env.example as tables: a `# ==` line opens a section
    (the comment right under it introduces the section), and the comment
    block above a variable describes it."""
    sections: list[dict[str, Any]] = []
    comment: list[str] = []
    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.strip()
        if line.startswith("# =="):
            sections.append({"title": line[4:].strip(), "intro": "", "rows": []})
            comment = []
        elif match := ENV_VAR.match(line):
            name, example = match.groups()
            if not sections or not comment:
                raise SystemExit(
                    f"gen_docs: .env.example: {name} needs a section and a comment"
                )
            sections[-1]["rows"].append(
                [code(name), code(example) if example else "—", " ".join(comment)]
            )
        elif line.startswith("#"):
            comment.append(line.lstrip("#").strip())
        else:
            if sections and not sections[-1]["rows"] and not sections[-1]["intro"]:
                sections[-1]["intro"] = " ".join(comment)
            comment = []
    return "\n\n".join(
        f"## {s['title']}\n\n"
        + (f"{s['intro']}\n\n" if s["intro"] else "")
        + table(["Variable", "Example", "What it does"], s["rows"])
        for s in sections
    )


# -- the glossary ------------------------------------------------------------

GLOSSARY_ROW = re.compile(r"^\| \*\*(.+?)\*\* \| (.+?) \| .+ \|$")


def glossary(_: str | None) -> str:
    """The charter's glossary for a reader: every term and its meaning,
    alphabetical. The charter's third column, the names to avoid, is for
    contributors and stays in CLAUDE.md."""
    text = CHARTER.read_text()
    start = text.index("[start:glossary]")
    end = text.index("[end:glossary]")
    terms = sorted(
        (
            m.groups()
            for line in text[start:end].splitlines()
            if (m := GLOSSARY_ROW.match(line))
        ),
        key=lambda term: term[0].lower(),
    )
    if not terms:
        raise SystemExit("gen_docs: no glossary rows in CLAUDE.md")
    return table(["Term", "Meaning"], [[f"**{t}**", meaning] for t, meaning in terms])


# -- the bounds -------------------------------------------------------------


def bounds(example: str | None) -> str:
    """The bounds every dispatch of the example's company carries, made
    the way the platform makes them: the example compiled, submitted as
    the Company CR, rendered from that spec."""
    document = yaml.safe_load((REPO / "examples" / f"{example}.yaml").read_text())
    spec = company_cr(compile_blueprint(document))["spec"]
    return "```\n" + render_bounds(spec) + "\n```"


GENERATORS: dict[str, Callable[[str | None], str]] = {
    "roles": roles,
    "crews": crews,
    "procedures": procedures,
    "tools": tool_table,
    "tool-count": tool_count,
    "cli": cli,
    "console": console_routes,
    "console-acts": console_acts,
    "audit-events": audit_events,
    "models": models,
    "workflows": workflow_table,
    "signals": signal_table,
    "blueprint-fields": blueprint_fields,
    "blueprint-sections": blueprint_sections,
    "policy-rules": policy_rules,
    "environment": environment,
    "glossary": glossary,
    "bounds": bounds,
}


def render(rel: str, text: str, used: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name, arg = match.group(1), match.group(2)
        if name not in GENERATORS:
            raise SystemExit(f"gen_docs: {rel} names unknown generator {name!r}")
        used.add(name)
        return GENERATORS[name](arg)

    return MARKER.sub(replace, text)


def render_pages() -> None:
    used: set[str] = set()
    for page in sorted(DOCS.rglob("*.md")):
        rel = page.relative_to(DOCS).as_posix()
        text = page.read_text()
        if rel.startswith("_") or not MARKER.search(text):
            continue
        with mkdocs_gen_files.open(rel, "w") as out:
            out.write(render(rel, text, used))
        mkdocs_gen_files.set_edit_path(rel, rel)
    unused = set(GENERATORS) - used
    if unused:
        raise SystemExit(f"gen_docs: no page uses {sorted(unused)}")


render_pages()
