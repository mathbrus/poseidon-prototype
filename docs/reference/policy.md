# Policy and audit

Every consequential action is decided by a compiled Rego bundle and leaves
exactly one audit event. This page names the vocabulary those two records
use: what a decision reads, what the rule names mean, and where the events
come from. The bundle is **generated** by the compiler from the blueprint
(`packages/compiler/src/compiler/rego.py`); nobody writes a rule by hand,
and `packages/compiler/tests/golden/` holds the compiled bundle for the
shipped example if you want to read the real thing.

## Bundles and decision paths

Two bundles exist. The platform bundle, `poseidon/platform/decision`,
grants the platform lifecycle its tools (`repo.archive`, `git.merge_pr`, and
the same tool names the workflows call on the company's behalf). Each
company bundle is its own package, `poseidon.authz.<company>`, at its own
decision path, which is why two companies never collide and why `platform`
is a reserved company name. The answer is always the same shape:

```json
{"allow": false, "rule": "default_deny"}
```

Everything is default-deny, and the lowest-sorting deny wins over any
allow. The `rule` is the name that lands in the audit row.

## What a decision reads

The MCP gateway builds the input; nothing in it comes from the caller's
own claims. Identity fields are the verified workload identity, the run
fields come from the registry, and `args` are the call's arguments.

| Field | Where it comes from |
|---|---|
| `company`, `agent_id`, `lifecycle`, `parent_agent_id`, `role` | The verified identity: a TokenReview on the caller's projected ServiceAccount token, resolved against the registry row that mints the identity (`agent_instances` for standing agents, `run_participants` for ephemeral ones). |
| `crew_template` | Ephemeral callers only, from their participants row. An ephemeral caller with no parent or crew is refused before policy runs. |
| `run_id`, `workflow` | The run the call names, and the workflow the registry says that run belongs to. |
| `run_binding_verified` | Whether the caller is verifiably bound to that run: an ephemeral agent's row names exactly one run; a platform caller needs a live sandbox registration; a standing agent's company namespace must own the registration. Every run-scoped rule requires it, so a wrong, stale or revoked claim fails closed. |
| `tool`, `args` | The call. For `git.open_pr` the gateway adds the changed paths it computed from the bundle, which is what makes path rules enforceable. |
| `active_spawns`, `budget_headroom` | `crew.request` only: the caller's live spawn count and whether the run is under its per-run cap. |

## The rule vocabulary

Rule names are stable and readable on purpose: an audit row's `rule` should
say why without a lookup.

| Rule | Decides |
|---|---|
| `default_deny` | Nothing matched. |
| `role_<standing>_<tool>` | A standing agent's compiled scope: its catalog tools narrowed by the blueprint's `tools.<role>`. |
| `crew_<crew>_<role>_<tool>` | An ephemeral agent's compiled scope, per crew and role; run-scoped tools add the `bound_to_run` clause. |
| `platform_<tool>` | The platform lifecycle's own tools. |
| `crew_request_over_cap`, `crew_request_no_budget` | `crew.request` beyond the conductor's `max_concurrent`, or with no per-run budget headroom. |
| `repo_not_declared` | A tool call naming any repository other than the blueprint's `stack.repo.url`. |
| `git_open_pr_protected_branch`, `git_open_pr_branch_pattern`, `git_open_pr_paths_missing`, `git_open_pr_denied_paths` | Branch protection, the allowed branch pattern, the changed paths absent from the input (fail closed), and `policies.deny_paths`. |
| `git_open_pr_contract_tree` | A role other than the declared contract author delivering into the contract tree. |
| `heartbeat_start_<workflow>` | The heartbeat may start this workflow (only those on `may_start`). |
| `heartbeat_start_over_concurrency`, `heartbeat_start_over_daily_cap`, `heartbeat_start_over_attempts` | A declared cap reached. |
| `heartbeat_start_counts_missing`, `heartbeat_start_spend_missing`, `heartbeat_start_attempts_missing` | The live counts absent from the input — absence denies. |
| `merge_resolve_delegated` | A policy-resolved merge: verdict passed, checks green, reviewer independent, every changed path in the delegated class, and the contract green when one is declared. |
| `merge_resolve_verdict_not_pass`, `merge_resolve_checks_not_green`, `merge_resolve_not_independent` | One of the platform facts failed. |
| `merge_resolve_paths_missing`, `merge_resolve_carved_out_path`, `merge_resolve_undelegated_path`, `merge_resolve_contract_path` | The changed paths absent, or touching a carve-out (`.github/`), an undelegated path, or the contract tree. |
| `merge_resolve_contract_not_green` | The base-ref contract tree, run against the PR head, is red. |

## Audit events

One model, one registry table, one ingestion contract: every producer
writes the same `AuditEvent` shape (`packages/common/src/common/audit.py`)
and one `run_id` query returns the whole story across sources. Arguments
are stored only as a digest — audit must never become a leak channel.

| `source` | Written by | For |
|---|---|---|
| `gateway` | the MCP gateway | every tool call: decision, rule, backend outcome |
| `llm-gateway` | the LLM gateway | every model call and its metered cost; budget stops |
| `sandbox-infra` | the egress proxy | every allowed and denied egress attempt (`egress_not_allowlisted`) |
| `process-gate` | the orchestrator | gate outcomes: merge and intake verdicts, escalations, expiries, budget pauses, `gate.heartbeat_attempts` exhaustion, the daily-cap halt |
| `process-lifecycle` | the orchestrator | crew fulfilment: `lifecycle.bind`, `lifecycle.revoke`, pod provisioning and destruction; review facts (`review.verdict`, `merge.resolve`); heartbeat decisions (`heartbeat.decide`, `heartbeat.start`, `heartbeat.tick`) |

Every event carries `company`, `lifecycle`, `parent_agent_id` and
`workflow` so attribution never has to be inferred from naming
conventions, and `decision` is always `allow` or `deny` — a gate that
expired, a budget that stopped a run, and a merge that fired are all
decisions on the record.
