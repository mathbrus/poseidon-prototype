"""Render the reference pages' tables from the code they describe.

mkdocs-gen-files runs this at build time (see mkdocs.yml). A page under
docs/ carries a line `<!-- gen: NAME -->` wherever a table belongs; this
script replaces the marker with the table rendered from the catalogs, the
gateway's tool definitions, the `poseidon` CLI, the Temporal workflows and
the blueprint JSON Schema, and re-emits the page. Nothing rendered here is
committed, so nothing here can drift from the code. A marker naming no
generator, a generator no page uses, or a tool without a description fails
the build.
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

from orchestrator import workflows
from orchestrator.cli.main import app as cli_app
from orchestrator.workflows.base import WORKFLOWS, workflow_id
from schema import tools
from schema.blueprint import BLUEPRINT_SCHEMA
from schema.catalog import CREW_TEMPLATES, PROCEDURES, ROLES

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
GATEWAY_APP = REPO / "packages/mcp-gateway/src/mcp_gateway/app.py"

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
            name = next(kw.value.value for kw in deco.keywords if kw.arg == "name")  # type: ignore[attr-defined]
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
    "repository": tools.REPO_TOOLS
    + tools.GIT_TOOLS
    + tools.REVIEW_TOOLS
    + tools.PLATFORM_TOOLS,
    "issues": tools.ISSUE_TOOLS,
    "crew": tools.CREW_TOOLS,
}


def tool_table(group: str | None) -> str:
    if group not in TOOL_GROUPS:
        raise SystemExit(f"gen_docs: unknown tool group {group!r}")
    defined = _tool_definitions()
    if set(defined) != set(tools.KNOWN_TOOLS):
        raise SystemExit(
            "gen_docs: the gateway's tools and schema.tools.KNOWN_TOOLS differ: "
            f"{sorted(set(defined) ^ set(tools.KNOWN_TOOLS))}"
        )
    rows = []
    for name in TOOL_GROUPS[group]:
        t = defined[name]
        holders = [r.name for r in ROLES.values() if name in r.tools]
        rows.append(
            [code(t.name), code(t.arguments), t.description, codes(holders) or "—"]
        )
    return table(["Tool", "Arguments", "What it does", "Catalog roles holding it"], rows)


def tool_count(_: str | None) -> str:
    return str(len(tools.KNOWN_TOOLS))


# -- the CLI ----------------------------------------------------------------


def _plain_help(cmd: Any, prog: str) -> str:
    """The command's `--help` as plain text. Typer overrides `format_help`
    with rich panels, so the click base class beneath it is called."""
    ctx = cmd.context_class(cmd, info_name=prog, terminal_width=80, max_content_width=80)
    formatter = ctx.make_formatter()
    base = next(c for c in type(cmd).__mro__ if c.__module__ != "typer.core")
    base.format_help(cmd, ctx, formatter)
    return formatter.getvalue().rstrip()


def cli(_: str | None) -> str:
    """Each command's own `--help`, verbatim, at a fixed width."""
    out: list[str] = []

    def walk(cmd: Any, prog: str) -> None:
        out.append(f"### `{prog}`\n\n```text\n{_plain_help(cmd, prog)}\n```")
        for name, sub in getattr(cmd, "commands", {}).items():
            walk(sub, f"{prog} {name}")

    walk(typer.main.get_command(cli_app), "poseidon")
    return "\n\n".join(out)


# -- the Temporal contract ---------------------------------------------------

_WORKFLOW_CLASSES: dict[str, tuple[type, type]] = {
    "scope": (workflows.ScopeWorkflow, workflows.ScopeInput),
    "implement": (workflows.ImplementWorkflow, workflows.ImplementInput),
    "review": (workflows.ReviewWorkflow, workflows.ReviewInput),
    "heartbeat": (workflows.HeartbeatWorkflow, workflows.HeartbeatInput),
}


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
        ("minimum", "≥"),
        ("exclusiveMinimum", ">"),
        ("minItems", "at least items:"),
        ("minProperties", "at least entries:"),
    ):
        if key in node:
            parts.append(f"{label} {node[key]}")
    if node.get("minLength") == 1 and "pattern" not in node:
        parts.append("non-empty")
    if node.get("uniqueItems"):
        parts.append("unique")
    return "; ".join(parts)


def _rows(node: dict[str, Any], path: str, required: bool) -> list[list[str]]:
    rows = [[code(path), _type_of(node), "yes" if required else "", _constraints(node)]]
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
    return table(["Field", "Type", "Required", "Constraints"], rows)


def blueprint_sections(_: str | None) -> str:
    top = BLUEPRINT_SCHEMA["properties"]
    rows = [
        [code(name), "yes" if name in BLUEPRINT_SCHEMA["required"] else ""]
        for name in top
    ]
    return table(["Section", "Required"], rows)


# -- the pages ---------------------------------------------------------------

GENERATORS: dict[str, Callable[[str | None], str]] = {
    "roles": roles,
    "crews": crews,
    "procedures": procedures,
    "tools": tool_table,
    "tool-count": tool_count,
    "cli": cli,
    "workflows": workflow_table,
    "signals": signal_table,
    "blueprint-fields": blueprint_fields,
    "blueprint-sections": blueprint_sections,
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
