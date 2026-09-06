# schema

The blueprint contract: what a company document may say, and the versioned
catalogs of roles, crews and tool names it may say it
with. This package answers "is this document well-formed?" — never "is this
company coherent?", which is the [compiler](../compiler/README.md)'s
question. The split is not cosmetic. Everything here can be checked against
the document alone, with no cluster, no registry snapshot and no deployment
plan — which is exactly what lets an author validate a blueprint on a
laptop, and lets every other package trust that a document it receives has
already survived this gate.

A blueprint is a YAML mapping with `apiVersion: poseidon.dev/v1` and eight
sections: `company`, `stack`, `standing`, `tools`, `workflows`,
`policies`, `budgets`, `heartbeat`. All are required except `policies` —
a company without guardrails is a choice you make visibly, not a default
you fall into by omission — and a company always has a heartbeat: it is
how work originates, and `company.vision` with `company.documents` is
what it originates from. `company.humans` names the people who may act
on the company through the console. The document format
itself is read section by section on [the blueprint reference](../../docs/reference/blueprint.md).

## Module map

| Module | Owns |
|---|---|
| `blueprint.py` | The JSON Schema (draft 2020-12) for the eight sections. Structure only; the tool enum is the one place it borrows from a catalog. |
| `validate.py` | `validate_blueprint` / `load_blueprint`. Errors are rendered as a path and the allowed values, and reported all together. |
| `coherence.py` | The catalog cross-checks (roles and crews exist, no platform-only tool for a role, scopes narrow the catalog), as functions both `validate` and the compiler run. |
| `catalog.py` | `ROLES` and `CREW_TEMPLATES`: what a blueprint may hire and spawn, with each role's tool scope, procedures and spawn authority. |
| `tools.py` | The 16-name gateway tool surface, plus the two sets policy keys on: `PLATFORM_ONLY` and `RUN_SCOPED`. |

## Two passes, one report

Validation runs a schema pass and a catalog pass, and always runs both —
a typo in `company` should not hide a bad role three sections
away (`validate_blueprint` says so in a comment, and
`test_every_problem_is_reported_together` pins it). Fixing a blueprint one
message per run is a bad trade for the author.

The schema pass rewrites `jsonschema`'s output, because the library's
default messages name a failing keyword deep in a combined schema, and
what an author needs is *where* in their document and *what to write
instead*. So an `enum` failure lists every allowed value, and an
`additionalProperties` failure — which is how a typo'd key surfaces,
since every object in the schema sets `additionalProperties: false` —
says "check the spelling, or the section it belongs to".

The catalog pass is kept deliberately narrow: only what a *document* can
be wrong about regardless of cluster state. It catches a `role` or a
`may_spawn` crew outside the catalogs, a platform-only tool in any
allow-list, and a tool scope wider than its role's catalog tools. Each check tolerates malformed values rather than crashing
on them — the schema pass already reports those — and simply skips what it
cannot read.

## The role catalog

Five roles, and the set is closed: a blueprint names roles, it never
defines them. A role is what an agent *is*: its tool ceiling, the crews
it may spawn, and the procedures a dispatch may ask of it. `pm` is the
standing conductor and holds no coding tools at all — no `sandbox.*`, no
`git.open_pr` — and no private memory store either: cross-run state is
tickets and registry runs. The other four are ephemeral-agent material
only. Which role holds which tools, runs which procedures and may spawn
which crews is rendered from `ROLES` on
[Roles and crews](../../docs/reference/roles.md).

Why a fixed catalog at all? Because the compiler turns each role into an
agent spec and a per-agent Rego bundle, and a role a blueprint could
invent would be a policy the platform never reviewed. Roles carry tool
scopes, procedures and spawn authority — never counts, never secrets: how many of a role a company hires
is the blueprint's business, and credentials never enter a spec at all.

The lean-standing rule — no standing `implementer`, `reviewer`,
`contract-author` or `steward` — is deliberately **not** checked here. Whether a hire
is a coder is a question about the company the plan would deploy, so it
is a compiler error, and `test_lean_roster_profile_validates` proves the
standing-coder document still validates structurally.

## The crew catalog

Four crews, one role each, rendered from `CREW_TEMPLATES` on the same
page. A crew is a list of roles, and the catalog is the only place that
list is written: a blueprint names the crews it puts to work (in a hire's
`may_spawn` and a workflow's `crew`), never their members — a name outside
the catalog is a document error rather than something left for the
compiler to resolve against nothing.

A blueprint may **narrow** a role's allow-list in `tools.<role>`, never
widen it — and the scope applies to every agent of the role, standing or
ephemeral. A document granting the reviewer `git.open_pr` fails
validation with a message quoting the catalog's list, because the
reviewer-cannot-deliver contract is a catalog property and a wider
document must fail here rather than compile into a wider bundle.
Narrowing, though, is fine: an implementer scoped to only
`sandbox.read_file` and `sandbox.run_tests` validates.

One asymmetry surprises people: an ephemeral agent's allow-list is
deliberately **not** required to be a subset of its conductor's — spawn
authority is authority to create a principal from a compiler-vetted role,
not a capability ceiling. [Standing vs. ephemeral agents](../../docs/concepts/standing-vs-ephemeral.md)
makes the argument.

## The tool catalog

`tools.py` names the entire gateway surface in groups (sandbox, repo,
git, issues, review, platform, crew) and folds them into `KNOWN_TOOLS`;
[Tools](../../docs/reference/tools.md) renders the list with each tool's
arguments. The schema embeds that set as the enum inside every
tool list, so `issues.delete` fails the *schema* pass with the real names
listed; nothing downstream ever meets an unregistered tool name.

Two subsets carry the package's sharpest teeth:

```python
# Tools no company role may declare: the platform identity holds them, and a
# blueprint that could grant them would make the merge gate optional.
PLATFORM_ONLY = frozenset(PLATFORM_TOOLS)

# Tools whose compiled rules additionally require a verified run binding in
# the decision input — a role allow-list without run binding reopens
# cross-run tool use. A property of the tool, not a name prefix.
RUN_SCOPED = frozenset(SANDBOX_TOOLS)
```

`PLATFORM_ONLY` is one tool, `git.merge_pr`, and validation refuses it in
any role or ephemeral-agent allow-list — that check lives here, not in the
compiler, because a document that grants it is wrong on its face. Note
what makes it interesting: the name *is* in `KNOWN_TOOLS`, so the schema
enum accepts it; the refusal is about who may hold it, not whether it
exists.

`RUN_SCOPED` is read by the compiler's Rego emitter, which appends a
`bound_to_run` clause to each such rule. Membership being a property of
the tool rather than a name prefix matters: `git.open_pr` is not
run-scoped, because a pull request targets the repository, not a run's
sandbox.

## Entry points

A library only — no service, no CLI, no Dockerfile. `validate_blueprint`
takes a parsed document and returns it, or raises `ValidationError`
carrying every problem as a `problems` list; `load_blueprint` adds YAML
parsing (a syntax error becomes a validation problem too, so callers
handle one exception type). The package `__init__` re-exports both plus
the schema and the catalogs.

## Cross-package interactions

- [`compiler`](../compiler/README.md) resolves every role and crew a blueprint
  names against `ROLES` / `CREW_TEMPLATES`, re-checks scopes
  against `KNOWN_TOOLS` and `PLATFORM_ONLY` in its own passes, and reads
  `RUN_SCOPED` when emitting Rego. It also owns everything this package
  refuses to check: workflow-conductor binding, budget coverage, the
  lean-standing rule.
- The [`console`](../console/README.md) (`preview`, `apply`) and
  `python -m compiler` call `load_blueprint` before compiling, so authors
  see these errors first.
- [`gateway`](../mcp-gateway/README.md) implements the same tool names.
  It does not import this package — the correspondence is kept by hand,
  so adding a tool means adding it in both places, this one first.

## Invariants

- Every object in the schema is `additionalProperties: false`: a typo'd
  key is an error, never a silently ignored line.
- Names (`company.name`, `company.humans`, standing names, agents) match
  `^[a-z0-9][a-z0-9-]*$`; roles are keyed by name so a blueprint
  diff — a hire, a fire — reads as one.
- An ephemeral agent's `tools` list is required, explicit, and at most the
  catalog agent's list. An implicit scope is what the compiler refuses
  to guess.
- No company role or ephemeral agent may hold a `PLATFORM_ONLY` tool.
- The catalogs themselves obey the tool rules: every template scope is a
  subset of `KNOWN_TOOLS` and disjoint from `PLATFORM_ONLY`
  (`test_catalog.py` pins both).

## Tests

```
uv run pytest packages/schema
```

Sub-second, no cluster. `test_blueprint.py` proves the shipped
`examples/hello-saas.yaml` validates and that representative invalid
documents fail with messages that say *where* and *what to write
instead* — the invalid cases assert on message content, not just on the
raising. `test_catalog.py` is contract tests, not coverage: each one is a
rule some other package assumes it can rely on. Run per-package only —
repo-wide pytest fails at collection by design.
