# Policy and audit

Every action that matters is decided by compiled policy and leaves one
audit event. This page lists what a decision reads, every rule it can
answer with, and every event the platform records.

## How a decision works

The compiler generates the policy from your blueprint, as a
[Rego](../concepts/opa.md) bundle. Nobody writes rules by hand. To read the
real thing, see the compiled bundle for the shipped example in
`packages/compiler/tests/golden/`.

Every decision answers in the same shape:

```json
{"allow": false, "rule": "default_deny"}
```

- Everything is denied unless a rule allows it.
- Any deny beats every allow. When several deny, the first in sort order
  names the result.
- The `rule` is what the audit row records.
- A denial's message also says what it refused, for example the branch and
  the pattern it had to match.

There are two bundles. The platform's (`poseidon/platform/decision`)
grants the platform's own tools. The company's
(`poseidon.authz.<company>`) holds everything compiled from the
blueprint. That's why `platform` is a reserved company name.

OPA polls a bundle server for both. Nothing is pushed. Until OPA serves
the company's bundle, every enforcement point denies with
`no_policy_installed`, and the company reads `activating`. When a company
is deleted, its bundle becomes an empty *tombstone*, and OPA's next poll
erases its rules.

## What a decision reads

The MCP gateway builds the input for a tool call, the orchestrator for its
own decisions, the console for a person's act. Nothing in it is taken from
the caller's own claims.

| Field | Where it comes from |
|---|---|
| `company`, `agent_id`, `lifecycle`, `parent_agent_id`, `role` | The caller's verified identity: its token, checked by TokenReview, resolved against its registry row. For a person's act: `lifecycle: human`, `role: human`, `agent_id` the person's name. |
| `crew_template` | An ephemeral agent's registry row. An ephemeral caller without a crew is refused before policy runs. |
| `run_id`, `workflow` | The run the call names, and the workflow the registry says it belongs to. |
| `run_binding_verified` | Whether the caller is bound to that run. Every run-scoped rule requires it, so a wrong, stale or revoked claim is denied. |
| `tool`, `args` | The call. For `git.open_pr`, the gateway adds the changed paths it computed from the bundle. |
| `args.paths_present` | `git.open_pr` only: which changed paths already exist on the base branch. |
| `active_spawns`, `budget_headroom` | `crew.request` only: the conductor's live crew count, and whether the run has budget left. |
| `architecture_requests` | `architecture.request` only: how many requests this run already filed. |

## Rules

Rule names are meant to explain themselves in an audit row. A
`<placeholder>` is filled in per company. This table is rendered from
`common.policy.RULES`, and the compiler's tests keep it equal to what the
compiler emits.

<!-- gen: policy-rules -->

## Audit events

Every component writes the same event shape to one table, so one query by
`run_id` returns a run's whole story.

- Arguments are stored only as a digest, so the audit trail can't leak
  them.
- The one cleartext field is `rationale`: the reason an actor gave for a
  decision that commits the company to something, up to 1,000 characters.
  A refusal carries none, because its rule is the reason.
- `decision` is always `allow` or `deny`. A gate that expired or a budget
  that parked a run are decisions on the record too.

| `source` | Written by | Records |
|---|---|---|
| `gateway` | the MCP gateway | every tool call: decision, rule, outcome |
| `llm-gateway` | the LLM gateway | every model call and its cost; budget refusals |
| `sandbox-infra` | the egress proxy | every allowed and denied egress attempt |
| `process-gate` | the orchestrator | gate outcomes: verdicts, escalations, expiries, budget parks, and halts |
| `process-lifecycle` | the orchestrator | crews formed, the review's facts, the heartbeat's decisions |
| `console` | the console | every act of a person, under their name, and every token it refused |

Gateway rows name the [tool](tools.md) that was called. Every other row
carries one of these names, from `common.audit`:

<!-- gen: audit-events -->
