# compiler

`(blueprint, role_catalog, registry_snapshot) → deployment plan`, as a pure
function. Every enforcement projection the platform runs on — custom
resources, Rego policy bundles, the infrastructure policy IR, crew
artifacts, budgets, gate config — comes out of this one call. That is what
makes "the blueprint is the source of truth" checkable rather than
aspirational: a governance mistake fails here, at compile, instead of at the
first tool call in production.

The compiler reads nothing but its arguments, writes nothing, and touches no
cluster. Applying a plan is the operator's job.

## Module map

| Module | Owns |
|---|---|
| `compile.py` | The entry point, and `PLATFORM_SCOPE` — what the orchestrator's own identity may call. Not blueprint-configurable: a company that could grant itself the platform lifecycle could merge its own pull requests. |
| `checks.py` | The validations the schema cannot make, because they are about a company being *coherent* rather than a document being well-formed. |
| `plan.py` | The plan dataclasses and a deterministic `as_dict()` — ordering fixed by construction so a plan equals itself after a round trip through a file. |
| `rego.py` | Policy bundle emission: default-deny `decision`, `allow_rules`/`deny_rules` as sets, lowest-sorting deny wins. |
| `infra.py` | The infrastructure policy IR: one IR from `stack` + policies, several projections (sandbox constraints, NetworkPolicy, egress allowlist). |
| `diff.py` | Preview: what applying this plan would change. Hire/fire is a diff against the registry snapshot. |
| `snapshot.py` | The registry snapshot the diff is taken against. |
| `errors.py` | `CompileError` — every problem at once, as actionable text. |
| `regolden.py` | Regenerates the golden files. Not a test. |

## Two bundles per apply

- **The company bundle**, `package poseidon.authz.<company_slug>` — per
  company, so two live companies never claim overlapping OPA bundle roots.
  Activation under a shared root would erase the other company's rules.
- **The platform bundle**, `package poseidon.platform` — the orchestrator's
  scope, compiler-owned and company-independent, so it compiles identically
  from every blueprint and every operator upsert lands the same revision.

The gateway derives the decision path from the caller, so its
`company_decision_path` and this package's `company_package` must produce
the same slug. A cross-package test in `packages/gateway/tests/` pins that;
divergence would fail closed but silently.

## The goldens are generated — never hand-edit them

`tests/golden/` holds the compiled plan and bundle for
`examples/hello-saas.yaml`. They are a review artifact: the point is that a
human reads the diff when policy output changes.

After **any** change to the generator — including the text of a comment
`rego.py` emits into the Rego:

```
uv run python -m compiler.regolden
uv run pytest packages/compiler
git diff packages/compiler/tests/golden/     # read it; confirm it matches intent
```

Regolden is deliberately separate from the test run so updating a golden is
an explicit act, never something a test does quietly on the way past.

## Entry points

A library, driven by the `poseidon blueprint` CLI in `orchestrator/cli/`
(`compile`, `preview`). No Dockerfile of its own; it ships inside the
orchestrator and operator images.

## Cross-package interactions

- Resolves templates and tool names against `schema`.
- Reads a `RegistrySnapshot` built from `registry` rows to keep agent ids
  stable across applies and to compute the firing half of a diff.
- Its output is consumed by `operator` (which applies it) and, through the
  applied blueprint in the registry, by `orchestrator` (SandboxPolicy,
  workflow and gate config) and the egress proxy (host allowlist).
- `rego.RUN_SCOPED` comes from `schema.tools`.

## Tests

```
uv run pytest packages/compiler
```

`test_purity.py` is the unusual one: it proves the compiler performs no I/O
beyond its arguments, so "apply is the only mutation of enforcement" is
mechanically checked. `test_bundle_decisions.py` loads the emitted bundles
into a real OPA and asserts decisions, so policy claims are tested as policy
rather than as strings.

## Invariants

- Pure: no cluster, no filesystem, no network.
- Secrets never enter a plan. Agent specs carry a role, a tier and a tool
  scope; credentials arrive at runtime as projected tokens.
- A blueprint narrows a template, never widens it. `checks.py` errors on a
  widening attempt and the emission intersects anyway — belt under brace.
- No child-⊆-parent scope check, deliberately: spawn authority is not a
  capability ceiling.
- Company names may not be `platform` — that key is the platform bundle's.
- Rego string literals are emitted with `json.dumps`, never raw
  interpolation: a policy value containing a backtick would otherwise
  inject policy text into the bundle.
