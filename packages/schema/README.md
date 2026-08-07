# schema

The blueprint contract: what a company document may say, and the versioned
catalogs of role and crew templates it may name. This package answers "is
this document well-formed?" — never "is this company coherent?", which is
the compiler's question. The split matters: everything here can be checked
against the document alone, with no cluster and no registry.

A blueprint has eight sections: `company`, `departments`, `crews`,
`workflows`, `tools`, `policies`, `budgets`, `stack`.

## Module map

| Module | Owns |
|---|---|
| `blueprint.py` | The JSON Schema for the eight sections. Structure only. |
| `validate.py` | `validate_blueprint` / `load_blueprint` — validation with errors a human can act on, reported together rather than one per run. |
| `catalog.py` | `ROLE_TEMPLATES` and `CREW_TEMPLATES`: what a blueprint may hire and spawn, with each template's tool scope, model tier and runtime hints. |
| `tools.py` | The gateway tool surface a blueprint may name, plus the two sets policy keys on: `PLATFORM_ONLY` and `RUN_SCOPED`. |

## The catalogs

Role templates: `pm` (the standing conductor — scopes goals, files tickets,
requests crews, and holds no executor tools at all), `backend-engineer` and
`sdet` (crew-member material only; a standing count of either is a compile
error), and `analyst` (a `count: 0` placeholder documenting a growth path,
structurally capped because it exercises no tools).

Crew templates: `feature-delivery` (a read-only planner plus one coding
executor) and `feature-review` (one reviewer working against a sandbox
provisioned from the PR ref).

A crew member's allow-list is deliberately **not** required to be a subset
of its conductor's. Spawn authority is not a capability ceiling — the PM
cannot open a pull request, yet the crew it spawns can.

## Two sets worth knowing

- `PLATFORM_ONLY` (`git.merge_pr`) — callable only by the orchestrator's
  platform identity, after the human merge gate. A blueprint that could
  grant it to a company role would make that gate optional, so validation
  refuses it.
- `RUN_SCOPED` (the `sandbox.*` tools) — the compiler emits an extra
  run-binding requirement for these. A per-role allow-list without run
  binding would reopen cross-run tool use. Membership is a property of the
  tool, not a name prefix.

## Entry points

A library only — no service, no CLI, no Dockerfile. `compiler` and the
`poseidon blueprint` CLI are its callers.

## Cross-package interactions

- `compiler` resolves every template named in a blueprint against these
  catalogs and checks every declared scope against `tools.KNOWN_TOOLS`, so a
  typo fails at compile rather than at the first tool call.
- `compiler.rego` reads `RUN_SCOPED` when emitting policy.
- `gateway` serves exactly the tool names defined here.

## Tests

```
uv run pytest packages/schema
```

`test_blueprint.py` proves the shipped `examples/hello-saas.yaml` validates
and that representative invalid documents fail with messages that say
*where* and *what to write instead* — the invalid cases assert on message
content, not just on raising. `test_catalog.py` pins the catalog's own
rules.

## Invariants

- Every object is `additionalProperties: false`, so a typo'd key is an
  error rather than a silently ignored line.
- The schema does not enforce the lean-roster rule (no standing executors).
  That is a compiler error, which is why a standing-executor document still
  validates structurally here — the test says so explicitly.
- Adding a tool means adding it here first; the compiler will not accept a
  scope naming anything outside `KNOWN_TOOLS`.
