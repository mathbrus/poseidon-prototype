# compiler

`compiler` turns a blueprint, the role and crew catalogs and a registry snapshot into a deployment plan, as a pure function. The [console](../console/README.md) calls it in-process for `preview` and `apply`, the CLI calls it from `poseidon blueprint new`, and CI and the docs build call it offline.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| library | — (ships inside the console image) | `compiler.compile_blueprint`, `python -m compiler BLUEPRINT [--out DIR]` | nothing | nothing |

## Module map

| Module | Owns |
|---|---|
| `compile.py` | `compile_blueprint()`, and `PLATFORM_SCOPE`: what the orchestrator's own identity may call. No blueprint can change it. |
| `checks.py` | `check()`: every company-level problem. |
| `plan.py` | `DeploymentPlan` and its parts, with a deterministic `as_dict()`. |
| `rego.py` | Both policy bundles: the company bundle and the platform bundle. |
| `infra.py` | The infrastructure IR: sandbox constraints, the NetworkPolicy projection and the egress-proxy allowlist. |
| `diff.py` | `diff_plan()`: the create, update and delete changes `preview` shows. |
| `snapshot.py` | `RegistrySnapshot`, a frozen value the caller builds from registry rows. `EMPTY` is a never-applied company. |
| `errors.py` | `CompileError`, which carries every problem as a `problems` list. |
| `__main__.py` | The offline compile: the plan on stdout, the Rego files in `--out`. |
| `regolden.py` | Regenerates the golden files. It is not a test. |

## How it works

### Compiling reads only its arguments

`compile_blueprint` touches no cluster, file or network. The console passes in a `RegistrySnapshot` read from the registry, then submits the plan as custom resources for the [reconciler](../reconciler/README.md) to converge. The snapshot keeps agent ids stable across applies: a converged hire keeps its id, and a new one gets `<standing_name>-1`.

The function does not run the schema pass. `check()` repeats the [blueprint](../blueprint/README.md) package's catalog, gate and document checks, so an unvalidated dict fails closed. It then adds the company-level checks:

- Only a role with spawn authority may be standing with `count: 1`.
- `heartbeat.conductor` is an active standing spawner, and may spawn every workflow's crew plus `stewardship`.
- `may_spawn` and tool scopes narrow the catalog. Widening either is an error.
- A scope no active hire or crew uses is an error.
- A per-run cap and its park deadline exist, the daily cap can fund one run, and every gate declares `deadline_minutes`.
- `stack.toolchain` declares `image` and `test_command`, and `allowed_pattern` compiles as a regex.

Every problem is collected into one `CompileError`. Emitted scopes are also intersected with the catalog.

### Every decision starts from default deny

Each bundle's entrypoint answers `{"allow": false, "rule": "default_deny"}` unless a rule matches. If any deny rule matches, the decision denies with `min(deny_rules)`, so the reported rule is deterministic. Only an empty deny set and a non-empty allow set allow. Each entrypoint declares an always-false `declared_nowhere` rule in both sets, so OPA loads a bundle with no deny rules.

The company bundle is `package poseidon.authz.<company_slug>`. It holds `authz.rego`, a `role_<standing>.rego` per hire, a `crew_<crew>_<role>.rego` per crew role, and `policies.rego`, `heartbeat.rego` and `console.rego`. It also holds `merge_gate.rego` when a merge gate is declared. The platform bundle, `package poseidon.platform`, holds `platform.rego` and is the same for every blueprint. Package names come from `common.policy`, where the gateways and the orchestrator also read them.

Rules over `blueprint.tools.RUN_SCOPED` tools end in `bound_to_run`, which requires a verified run binding in the input. Path rules over `git.open_pr` fail closed: a call without `args.paths` hits `git_open_pr_paths_missing`. Counts such as `active_spawns` arrive in the input, because OPA reads no live data. String values are emitted with `json.dumps`, so a value cannot inject Rego. [The policy reference](../../docs/reference/policy.md) lists every rule, and [the merge gate](../../docs/explanation/merge-gate.md) explains its rules.

### Golden files are generated and never hand-edited

`tests/golden/` holds the compiled plan (`hello-saas.plan.json`) and both bundles (`hello-saas-bundle/`, ten `.rego` files) for `examples/hello-saas.yaml`. After any change to what the compiler emits, including a comment written into Rego, regenerate them and read the diff:

```bash
uv run python -m compiler.regolden
uv run pytest packages/compiler
git diff packages/compiler/tests/golden/   # check it matches your intent
```

Regenerating is a separate step, so a widened scope shows up as a Rego diff someone reads.

## Invariants

- Compiling writes no file and opens no socket: `tests/test_purity.py::test_compiling_writes_nothing_and_calls_nobody`.
- Compiling does not mutate its input: `tests/test_purity.py::test_compiling_does_not_mutate_its_input`.
- The same input compiles to the same plan: `tests/test_plan.py::test_compilation_is_deterministic`.
- No secret enters a plan: `tests/test_plan.py::test_agent_specs_carry_no_secrets`.
- A standing implementer or steward is a compile error: `tests/test_checks.py::test_standing_implementer_is_a_compile_error`, `::test_a_standing_steward_is_a_compile_error`.
- Spawn authority cannot be widened: `tests/test_checks.py::test_spawn_authority_cannot_be_widened`.
- An ephemeral agent's tools need not be a subset of its conductor's: `tests/test_checks.py::test_child_scope_need_not_be_a_subset_of_the_conductor`.
- Every emitted rule is in `common.policy.RULES`, and every registered rule is emitted by some example: `tests/test_rule_registry.py`.
- Sandbox tools require a verified run binding: `tests/test_bundle_decisions.py::test_sandbox_tools_require_a_verified_run_binding`.
- Only the platform may merge: `tests/test_bundle_decisions.py::test_only_the_platform_may_merge`.
- A backtick in a branch pattern cannot inject policy: `tests/test_bundle_decisions.py::test_pattern_with_a_backtick_cannot_inject_policy`.

## Testing

```bash
uv run pytest packages/compiler
```

Run tests per package, as [Contributing](../../docs/developing/contributing.md) explains. `test_bundle_decisions.py` loads the emitted bundles into a real OPA, `openpolicyagent/opa:1.19.0`, through the `opa` binary or Docker. It skips when neither exists. CI also runs `opa check` on the offline compile of `examples/hello-saas.yaml`. On kind, the e2e suite runs every gateway call against the compiled bundles.

## Related

- [OPA and Rego](../../docs/concepts/opa.md) and [the policy reference](../../docs/reference/policy.md)
- [How it works](../../docs/explanation/how-it-works.md)
- [blueprint](../blueprint/README.md): the schema, catalogs and tool names this package compiles from
