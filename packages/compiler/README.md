# compiler

`(blueprint, role_catalog, registry_snapshot) → deployment plan`, as a pure
function. Every enforcement projection the platform runs on — custom
resources, Rego policy bundles, the infrastructure policy IR, crew
artifacts, budgets, gate config — comes out of this one call. That is what
makes "the blueprint is the source of truth" checkable rather than
aspirational: a governance mistake fails here, at compile, instead of at the
first tool call in production.

The compiler reads nothing but its arguments, writes nothing, and touches
no cluster. Applying a plan is the operator's job. If the vocabulary here
is new — blueprints, OPA, run binding — read the
[root README](../../README.md) first (sections 2, 4 and 7).

## Module map

| Module | Owns |
|---|---|
| `compile.py` | The entry point, and `PLATFORM_SCOPE` — what the orchestrator's own identity may call. Not blueprint-configurable: a company that could grant itself the platform lifecycle could merge its own pull requests. |
| `checks.py` | The validations the schema cannot make, because they are about a company being *coherent* rather than a document being well-formed. |
| `plan.py` | The plan dataclasses and a deterministic `as_dict()` — ordering fixed by construction so a plan equals itself after a round trip through a file. |
| `rego.py` | Policy bundle emission: default-deny `decision`, `allow_rules`/`deny_rules` as sets, lowest-sorting deny wins. |
| `infra.py` | The infrastructure policy IR: one IR from `stack` + policies, several projections (sandbox constraints, NetworkPolicy, egress allowlist). |
| `diff.py` | Preview: what applying this plan would change. Hire/fire is a diff against the registry snapshot. |
| `snapshot.py` | The registry snapshot the diff is taken against — a value, never a connection. |
| `errors.py` | `CompileError` — every problem at once, as actionable text. |
| `regolden.py` | Regenerates the golden files. Not a test. |

## What "pure function" buys, concretely

Purity here is not aesthetics; each consequence is load-bearing.

- **Determinism.** Same inputs, byte-identical plan
  (`test_plan.py::test_compilation_is_deterministic`). The preview diff
  is only meaningful if nothing wobbles between two compiles, and golden
  testing is only possible because output never depends on a clock, a
  cluster, or iteration order — `as_dict()` sorts by construction.
- **One mutation point.** Because compiling cannot touch the cluster,
  *apply* is the only place enforcement changes, and it is a separate,
  visible act (`poseidon blueprint apply` = compile + submit CRs).
- **Testability without infrastructure.** Even the diff needs no
  database: `snapshot.py` defines `RegistrySnapshot` as a frozen value
  the caller (the CLI) builds from registry rows and hands in.

The claim is enforced, not asserted. `test_purity.py` monkeypatches
`builtins.open` so that any write-mode open (`w`, `a`, `x`, `+`) raises,
and replaces `socket.socket` / `socket.create_connection` with functions
that fail on call — then compiles the example blueprint under that
harness. A second test deep-copies the input document and asserts
compilation left it untouched, so a caller can compile twice, or compile
a variant, without the first pass having edited the dict underneath it.

## One field, many artifacts: tracing `may_spawn`

The best way to see what the compiler is *for* is to follow one blueprint
field through everything it becomes. In `examples/hello-saas.yaml`:

```yaml
pm:
  role: pm
  count: 1
  may_spawn:
    crew: [feature-delivery, feature-review]
    max_concurrent: 2
```

The `may_spawn` block fans out into:

1. **Coherence checks.** `_spawn_authority` errors if the blueprint
   names a crew the `pm` catalog template does not grant — an error, not
   a silent drop, because a workflow may depend on the grant.
   `_spawnable_crews` errors if an enabled workflow's crew is unknown to
   the catalog or outside its conductor's `may_spawn` (it would compile
   but never be fulfillable).
2. **The `AgentInstanceCR`**, which carries `may_spawn` and
   `max_concurrent_spawns` into the AgentInstance custom resource.
3. **An allow rule** in the emitted `role_pm.rego` that narrows the tool's
   *arguments*, not just its name — spawn intent outside the list is denied
   before anything is recorded:

   ```rego
   allow_rules contains "role_pm_crew_request" if {
   	input.lifecycle == "standing"
   	input.company == "hello-saas"
   	input.role == "pm"
   	input.tool == "crew.request"
   	input.args.crew_template in ["feature-delivery", "feature-review"]
   }
   ```

4. **A deny rule per instance** enforcing the concurrency cap:

   ```rego
   deny_rules contains "crew_request_over_cap" if {
   	input.tool == "crew.request"
   	input.agent_id == "pm-1"
   	input.active_spawns >= 2
   }
   ```

Two things in that last rule teach the design. `input.active_spawns`
rides the *input* — the gateway counts live spawns and injects the
number — because OPA reads no live data: policy stays a pure function of
bundle + input, and restarts are safe. And the denial is *named*, so a
refused spawn is a first-class governance event in the audit log, not a
default-deny shrug.

## How a decision is made

Every bundle starts from the same entrypoint (`_DECISION` in `rego.py`):
a `default decision` of `{"allow": false, "rule": "default_deny"}`, with
`allow_rules` and `deny_rules` as partial sets that merge across files.
If any deny matches, the decision is deny and reports `min(deny_rules)`
— the lowest-sorting name — so which rule "won" is deterministic and
assertable. Only when the deny set is empty does a non-empty allow set
allow. One emitted oddity looks like dead code: each entrypoint declares
`allow_rules contains "declared_nowhere" if { false }` (and likewise for
`deny_rules`). A blueprint configuring no deny-producing policies would
otherwise emit a bundle where `deny_rules` is never declared, which OPA
refuses to load (unsafe variable); an always-false rule contributes
nothing while making the entrypoint safe for every blueprint.

### Why `bound_to_run` appears on sandbox rules only

Compare the implementer's `git.open_pr` rule with its `sandbox.exec` rule
in the goldens: only the latter ends in `bound_to_run`, a predicate
requiring `input.run_binding_verified == true` — a flag the gateway sets
after checking the caller's run binding against the registry. Sandbox
tools address *a specific run's* workspace, so a role allow-list alone
would quietly permit cross-run access: any implementer could read any run's
files. `git.open_pr` targets the repository, not a run, so run binding
is the wrong guard — its guards are the deny rules below. Which tools
carry the requirement is declared in `schema.tools.RUN_SCOPED`, a
property of the tool, not of its name prefix.

### The blueprint's own policies

`_policies_file` compiles `policies:` into deny rules over real
arguments: protected branches (lowercased at emit time, so
`protected: [Main]` still denies `main`), a branch-name regex, denied
path prefixes, path ownership keyed on role *templates*, and — when
`contract_tests` is declared — the split oracle: an acceptance-test tree
authored by a crew other than the implementer, so the tests that grade a
change and the change itself never share an author. The contract tree emits
two class-named rules: `git_open_pr_contract_tree` denies a
`git.open_pr` touching the tree for any role outside its
declared owners (the same `startswith`-over-prefix emission as path
ownership, reused), and `merge_resolve_contract_path` denies a policy
merge whose diff touches the tree whatever the facts say, so the oracle
never rides the delegation it certifies. The tree also joins the merge
carve-out set (`UNDELEGATABLE_PATHS ∪ deny_paths ∪ contract_tests.paths`,
composed at the one point `_merge_gate_file` already owns), and when it
is declared the merge allow additionally requires `input.args.contract_green`
with a fail-closed `merge_resolve_contract_not_green` deny. Rules are
named by class, not by index, because `min(deny_rules)` reporting makes
the rule name the audit row's reason. Path rules
fail closed: a `git.open_pr` whose input carries no `args.paths` hits
`git_open_pr_paths_missing`, because a rule that cannot be evaluated
must not default to allow. String values are emitted with `json.dumps`,
never raw interpolation, so a pattern containing a backtick cannot
terminate the raw Rego literal and splice text into the bundle
(`test_pattern_with_a_backtick_cannot_inject_policy` pins it).
`allowed_pattern` is also regex-validated at compile time — a malformed
pattern would otherwise surface as an OPA bundle-load failure long after
apply. (Python's `re` is not OPA's RE2, but it rejects the same broken
syntax an author actually writes.)

## Two bundles per apply

- **The company bundle**, `package poseidon.authz.<company_slug>` — per
  company, so two live companies never claim overlapping OPA bundle roots.
  Activation under a shared root would erase the other company's rules.
- **The platform bundle**, `package poseidon.platform` — the orchestrator's
  scope, compiler-owned and company-independent, so it compiles identically
  from every blueprint and every operator upsert lands the same revision.

The package name, and the slug transform inside it, come from
`common.policy` — the same module the gateway and the orchestrator derive
their decision paths from, so a bundle is always written where it is read.

## Coherence checks as a design surface

`checks.check()` runs every coherence pass and returns *every* problem, which
`CompileError` renders as one list — so a broken blueprint is fixed in one
round trip, not one error per attempt. The messages are written to be
sufficient to fix the document: what is wrong, where, and what is allowed
instead. One real example (the lean-standing rule, from `test_checks.py`):

```
standing.implementer: standing count 1 on non-spawner role 'implementer' — execution is ephemeral crews; declare a conductor with may_spawn, or set count: 0
```

Two subtleties. First, `compile_blueprint` accepts plain dicts and does
not run the schema pass, so the document-level checks (template
existence, tool vocabulary, the catalog cross-checks) run here too — the
same functions `schema.validate` runs, imported from `schema.coherence`,
because the compiler must fail closed on an unvalidated input, not
`KeyError` halfway through emission. Second, narrowing semantics differ by field:
widening a *ephemeral agent's* tools or a role's *spawn authority* past the
catalog is a compile error, while a standing hire's tool scope is
silently intersected with its role (`declared & set(role.tools)`
in `compile.py`) — and crew tools are intersected too, belt under the
brace, so nothing wider than the catalog can reach a bundle even if a
check regresses.

Deliberately absent: any child-⊆-parent scope check. The PM cannot open
a pull request, yet the crew it spawns can. Spawn authority is authority
to *create a principal* from a compiler-vetted template, not a
capability ceiling — keeping ⊆ would force the conductor's scope to
become the union of everything it might spawn, the opposite of least
privilege (`test_child_scope_need_not_be_a_subset_of_the_conductor`).

## The goldens are generated — never hand-edit them

`tests/golden/` holds the compiled plan (`hello-saas.plan.json`) and both
bundles (seven `.rego` files, company and platform side by side) for
`examples/hello-saas.yaml`. They are a review artifact: the point is that a
human reads the diff when policy output changes.

After **any** change to the generator — including the text of a comment
`rego.py` emits into the Rego:

```
uv run python -m compiler.regolden
uv run pytest packages/compiler
git diff packages/compiler/tests/golden/     # read it; confirm it matches intent
```

Regolden is deliberately separate from the test run so updating a golden
is an explicit act, never something a test does quietly on the way past.
A self-updating golden would pin nothing — the emitter always agrees with
its own output — and a scope-widening bug would sail through as a green
run instead of surfacing as a readable Rego diff.

## Entry points

A library, driven by the `poseidon blueprint` CLI in `orchestrator/cli/`
(`compile`, `preview`, `apply` — apply is exactly compile + CR submission).
No Dockerfile of its own; it ships inside the orchestrator image.

## Cross-package interactions

- Resolves templates and tool names against `schema`
  ([README](../schema/README.md)): the role/crew catalogs, `KNOWN_TOOLS`,
  `PLATFORM_ONLY`, and `RUN_SCOPED`.
- Reads a `RegistrySnapshot` built from `registry` rows to keep agent ids
  stable across applies (existing ids are reused in order; minted ids skip
  anything taken) and to compute the firing half of a diff.
- Its plan rides the custom resources the CLI submits — the Company CR
  carries the bundles, infra IR, workflows, budgets and crew specs — and
  the `operator` ([README](../operator/README.md)) reconciles them. From
  the applied blueprint, the `orchestrator` reads SandboxPolicy, workflow
  and gate config, and the egress proxy its host allowlist: one
  `egress_allow` list, two consumers, no drift.

## Tests

```
uv run pytest packages/compiler
```

Run per package. `test_purity.py` is the
unusual one: it proves the compiler performs no I/O beyond its
arguments, so "apply is the only mutation of enforcement" is
mechanically checked. `test_bundle_decisions.py` loads the emitted
bundles into a real OPA — pinned to the tag the chart deploys, via the
`opa` binary or Docker, skipping when neither is present — and asserts
*decisions*: golden tests pin what is emitted, decision tests pin what
it decides, which is the part that matters.

## Invariants

- Pure: no cluster, no filesystem, no network — and no mutation of the
  input document.
- Secrets never enter a plan. Agent specs carry a role, a tool scope, the
  company's repository and mission; credentials arrive at runtime as
  projected tokens
  (`test_plan.py` greps the whole plan for secret-shaped strings).
- A blueprint narrows the catalog, never widens it: widened crew tools and
  spawn authority are compile errors, and every emitted scope is
  intersected with its template regardless.
- No child-⊆-parent scope check, deliberately: spawn authority is not a
  capability ceiling.
- Company names may not be `platform` — that key is the platform bundle's,
  and a company by that name would shadow the platform decision path.
- Rego string literals are emitted with `json.dumps`, never raw
  interpolation: a policy value containing a backtick would otherwise
  inject policy text into the bundle.
