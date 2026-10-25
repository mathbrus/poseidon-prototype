# blueprint

`blueprint` defines what a blueprint may say: the JSON Schema, the role and crew catalogs, the gateway tool names, and the validator that checks a document against them. The [compiler](../compiler/README.md), the [console](../console/README.md), the CLI, the orchestrator, the agent host and the [mcp-gateway](../mcp-gateway/README.md) import it in-process.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| library | — (installed into its users' images) | `blueprint.load_blueprint`, `blueprint.validate_blueprint`, `blueprint.render_bounds` | nothing | nothing |

## Module map

| Module | Owns |
|---|---|
| `schema.py` | `BLUEPRINT_SCHEMA`, the JSON Schema (draft 2020-12) for `apiVersion: poseidon.dev/v1` and its eight sections. |
| `validate.py` | `validate_blueprint()`, `load_blueprint()` and `ValidationError`, which carries every problem as a `problems` list. |
| `coherence.py` | Catalog cross-checks: hires name catalog roles and crews, tool scopes narrow their role, no platform-only tool, contract owners are catalog roles. |
| `gates.py` | Merge-gate, contract-tree and document-path checks, and `NEVER_AUTOMATED_PATHS` (`.github/`). |
| `catalog.py` | `ROLES`, `CREW_TEMPLATES`, `PROCEDURES`, `STEWARD_ROLE` and `STEWARDSHIP_CREW`. |
| `tools.py` | The 20 gateway tool names (`KNOWN_TOOLS`) and the subsets policy keys on: `PLATFORM_ONLY`, `RUN_SCOPED`, `RATIONALE_TOOLS`. |
| `documents.py` | The two company documents by name (`strategy`, `architecture`) and the label a model sees each under. |
| `bounds.py` | `render_bounds(spec)`: the [bounds](../../docs/reference/glossary.md) text every dispatch carries into the system prompt. |

## How it works

### Validation reports every problem at once

`validate_blueprint` runs all its passes on every call and raises one `ValidationError` with every problem. The passes are:

1. The schema pass. Each `jsonschema` error is rewritten as a document path plus what to write instead. An `enum` failure lists the allowed values. An unknown key says to check the spelling, because every object in the schema sets `additionalProperties: false`.
2. The catalog pass (`coherence.py`): roles, crews, tool scopes, contract owners.
3. The gate and document passes (`gates.py`): no path in a merge gate's `automate` block overlaps `.github/`, `deny_paths` or the contract tree. The two document paths must be distinct, inside the repository, and outside `deny_paths` and the contract tree.

Each check skips values it cannot read, because the schema pass already reports them. `load_blueprint` adds YAML parsing, and a syntax error becomes a validation problem too. The compiler imports the same check functions, because it accepts dicts that never went through `validate_blueprint`.

This package checks only what the document alone can get wrong. Anything that needs the whole company belongs to the compiler: the conductor, budget coverage, and the rule that only spawner roles may be standing. Every schema field has a `description`, and the docs build renders [the blueprint reference](../../docs/reference/blueprint.md) from them. No field has a default.

### A blueprint names catalog roles and never defines them

The catalog holds five roles and four crews, rendered on [Roles and crews](../../docs/reference/roles.md). A role fixes its tool ceiling, the procedures it may run, and the crews it may spawn. `pm` is the only role with spawn authority, and it holds issue tools only. `implementer`, `reviewer`, `contract-author` and `steward` exist as ephemeral agents only. Each crew holds one role.

`tools.<role>.allow` may narrow a role's catalog tools for every agent of that role, standing or ephemeral. A scope that widens the catalog fails validation and quotes the allowed list. An ephemeral agent's tools need not be a subset of its conductor's. See [standing vs. ephemeral agents](../../docs/concepts/standing-vs-ephemeral.md).

### Tool names carry their policy class

`KNOWN_TOOLS` is the enum inside every tool list in the schema, so an unknown name fails the schema pass with the real names listed. [Tools](../../docs/reference/tools.md) renders the list with each tool's arguments. Three subsets carry policy:

- `PLATFORM_ONLY`: the platform's own tools, such as `git.merge_pr`, `issues.close`, `git.close_pr` and the `repo.*` reads. No role may declare one, so the merge gate cannot be granted away.
- `RUN_SCOPED`: the sandbox tools and `architecture.request`. The compiler adds a verified run-binding clause to each of their rules. `git.open_pr` is not run-scoped, because a pull request targets the repository, not a run's sandbox.
- `RATIONALE_TOOLS`: the tools whose call commits the company to something, so the call records the caller's rationale.

## Invariants

- Every problem is reported in one error: `tests/test_blueprint.py::test_every_problem_is_reported_together`.
- A typo'd key is an error, never ignored: `tests/test_blueprint.py::test_typo_in_a_key_is_rejected_not_ignored`.
- A tool scope narrows the catalog and never widens it: `tests/test_blueprint.py::test_a_tool_scope_cannot_widen_the_catalog`.
- No role, in the catalog or a blueprint, holds a platform-only tool: `tests/test_catalog.py::test_no_role_carries_a_platform_only_tool`, `tests/test_blueprint.py::test_company_role_cannot_hold_the_platform_merge_tool`.
- Every catalog tool is a known tool: `tests/test_catalog.py::test_every_role_scope_is_a_known_tool`.
- The implementer can deliver and the reviewer cannot: `tests/test_catalog.py::test_implementer_can_deliver_and_reviewer_cannot`.
- `.github/`, `deny_paths` and the contract tree are never automated: `tests/test_blueprint.py::test_ci_configuration_can_never_be_automated`, `::test_a_deny_path_can_never_be_automated`, `::test_the_contract_tree_can_never_be_automated`.
- A standing implementer still validates, because the compiler owns that rule: `tests/test_blueprint.py::test_a_standing_implementer_still_validates`.
- Every shipped example declares a heartbeat: `tests/test_blueprint.py::test_every_example_declares_a_heartbeat`.
- The bounds keep one line per section in a fixed order, and an absent fact says so: `tests/test_bounds.py`.
- The gateway's tools and `KNOWN_TOOLS` are the same set: checked by `scripts/gen_docs.py` during `uv run mkdocs build`.

## Testing

```bash
uv run pytest packages/blueprint
```

The suite runs in under a second with no cluster. It validates every file in `examples/` and asserts on message content, not only on the raise. Run tests per package, as [Contributing](../../docs/developing/contributing.md) explains.

## Related

- [Blueprint reference](../../docs/reference/blueprint.md), [Roles and crews](../../docs/reference/roles.md), [Tools](../../docs/reference/tools.md)
- [The merge gate](../../docs/explanation/merge-gate.md)
- [compiler](../compiler/README.md): company-level checks and policy emission
- [mcp-gateway](../mcp-gateway/README.md): serves the tools named here
