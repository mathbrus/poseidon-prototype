# schema

The blueprint contract: what a company document may say, and the versioned
catalogs of role templates, crew templates and tool names it may say it
with. This package answers "is this document well-formed?" — never "is this
company coherent?", which is the [compiler](../compiler/README.md)'s
question. The split is not cosmetic. Everything here can be checked against
the document alone, with no cluster, no registry snapshot and no deployment
plan — which is exactly what lets an author validate a blueprint on a
laptop, and lets every other package trust that a document it receives has
already survived this gate.

A blueprint is a YAML mapping with `apiVersion: poseidon.dev/v1` and nine
sections: `company`, `stack`, `standing`, `crews`, `tools`, `workflows`,
`policies`, `budgets`, `conduct`. All are required except `policies` and
`conduct` — a company without guardrails is a choice you make visibly, not
a default you fall into by omission, and a company with no standing
heartbeat is simply one no heartbeat drives. The document format itself is introduced line by line in
[docs/blueprint.md](../../docs/blueprint.md).

## Module map

| Module | Owns |
|---|---|
| `blueprint.py` | The JSON Schema (draft 2020-12) for the nine sections. Structure only; the tool enum is the one place it borrows from a catalog. |
| `validate.py` | `validate_blueprint` / `load_blueprint`. Errors are rendered as a path and the allowed values, and reported all together. |
| `coherence.py` | The catalog cross-checks (templates exist, crews narrow the catalog, no platform-only tool for a role), as functions both `validate` and the compiler run. |
| `catalog.py` | `ROLE_TEMPLATES` and `CREW_TEMPLATES`: what a blueprint may hire and spawn, with each template's tool scope, model tier and runtime hints. |
| `tools.py` | The 16-name gateway tool surface, plus the two sets policy keys on: `PLATFORM_ONLY` and `RUN_SCOPED`. |

## Two passes, one report

Validation runs a schema pass and a catalog pass, and always runs both —
a typo in `company` should not hide a bad role template three sections
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
be wrong about regardless of cluster state. It catches a `template` or
`crews:` key outside the catalogs, an ephemeral agent the template does not
define, an agent whose `role_template` contradicts the catalog's, a
platform-only tool in any allow-list, and an agent allow-list wider than
the catalog's. Each check tolerates malformed values rather than crashing
on them — the schema pass already reports those — and simply skips what it
cannot read.

## The role catalog

Four templates, and the set is closed: a blueprint names templates, it
never defines them. `pm` is the standing conductor — it holds the four
`issues.*` tools plus `crew.request`, may spawn crews, and holds no
executor tools at all: no `sandbox.*`, no `git.open_pr`. Its
`runtime_hints` include `memory: "none"`, because cross-run state is
tickets and registry runs, not a private store. `backend-engineer`
(sandbox tools plus `git.open_pr`, frontier tier), `sdet` (sandbox plus
review tools plus `issues.comment`, standard tier) and `contract-author`
(sandbox tools plus `git.open_pr`, frontier tier — the one template that
may deliver into a declared contract tree) are ephemeral-agent material
only.

Why a fixed catalog at all? Because the compiler turns each template into
an agent spec and a per-agent Rego bundle, and a template a blueprint
could invent would be a policy the platform never reviewed. The catalog
is versioned (`CATALOG_VERSION = "v1"`) so that resolution is against a
known artifact, and templates carry tool scopes, model tier and runtime
hints — never counts, never secrets: how many of a role a company hires
is the blueprint's business, and credentials never enter a spec at all.

The lean-standing rule — no standing `backend-engineer` or `sdet` — is
deliberately **not** checked here. Whether a standing role is an executor is
a question about the company the plan would deploy, so it is a compiler
error, and `test_lean_roster_profile_validates` proves the
standing-executor document still validates structurally.

## The crew catalog

Three crew templates: `feature-delivery` (a planner plus one coding
executor), `feature-review` (one reviewer, run against a sandbox
provisioned from the PR ref) and `contract-authoring` (one contract
author, no planner — the contract ticket is the brief). Crew names,
agent names and each agent's `role_template` in a blueprint must all
match the catalog: a `crews:` key outside it would validate structurally
and then resolve against nothing in the compiler, which is the failure
mode these cross-checks exist to kill.

The planner is the catalog's most deliberate agent:

```python
# The planner is LLM-only: read-only against the run's one sandbox, no
# sandbox of its own, no write tools. It produces
# exactly one artifact — the task brief the executor's dispatch carries.
_PLANNER_TOOLS = ("sandbox.read_file", "sandbox.list_dir", "issues.read")
```

`needs_sandbox: False` on that agent is the other half of the same
contract: LLM-only agents get no sandbox of their own.

A blueprint may **narrow** a catalog agent's allow-list, never widen it.
A document granting the planner `sandbox.write_file` fails validation
with a message quoting the catalog's list — because the
planner-is-read-only contract is a catalog property, and a wider document
must fail here rather than compile into a wider bundle. Narrowing,
though, is fine: an executor declared with only `sandbox.read_file` and
`sandbox.run_tests` validates.

One asymmetry surprises people: an ephemeral agent's allow-list is
deliberately **not** required to be a subset of its conductor's. The PM
cannot open a pull request, yet the crew it spawns can. Spawn authority
is authority to create a principal from a compiler-vetted template, not a
capability ceiling — requiring ⊆ would force the PM's scope to become the
union of everything it might ever spawn, the opposite of least privilege.

Crews also carry `budget_inheritance: "run"`, the only value the schema
admits: agents inherit the run's budget, and the ledger keeps them as
separate dimensions so crew fan-out stays attributable.

## The tool catalog

`tools.py` names the entire gateway surface — 16 names in seven groups
(sandbox, repo, git, issues, review, platform, crew) — and folds them
into `KNOWN_TOOLS`. The schema embeds that set as the enum inside every
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

- [`compiler`](../compiler/README.md) resolves every template a blueprint
  names against `ROLE_TEMPLATES` / `CREW_TEMPLATES`, re-checks scopes
  against `KNOWN_TOOLS` and `PLATFORM_ONLY` in its own passes, and reads
  `RUN_SCOPED` when emitting Rego. It also owns everything this package
  refuses to check: workflow-conductor binding, budget coverage, the
  lean-standing rule.
- `orchestrator`'s `poseidon blueprint` CLI calls `load_blueprint` before
  compiling, so authors see these errors first.
- [`gateway`](../mcp-gateway/README.md) implements the same 17 tool names.
  It does not import this package — the correspondence is kept by hand,
  so adding a tool means adding it in both places, this one first.

## Invariants

- Every object in the schema is `additionalProperties: false`: a typo'd
  key is an error, never a silently ignored line.
- Names (`company.name`, standing roles, crews, agents) match
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
