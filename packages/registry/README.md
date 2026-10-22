# registry

The registry is the Postgres state the platform services share: nine
tables, each behind an accessor class. It is a library: each service
imports it and connects with its own `REGISTRY_DSN`.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| library | none | `registry.open_registry()` | nothing (callers hold `REGISTRY_DSN`) | the nine tables below, through their accessors |

## Module map

| Module | Owns |
|---|---|
| `db.py` | `Registry`: the pool, `migrate()`, and `now()`, the database clock a reader takes as a fence. `open_registry()` opens and migrates. |
| `schema.sql` | The nine tables and their indexes, all `IF NOT EXISTS`. |
| `companies.py` | `Companies`: the compiled spec and the pause. `applied()` raises if two companies are applied. |
| `instances.py` | `AgentInstances`: the standing agents. |
| `policy.py` | `pack()` and `tombstone()` build bundle tarballs. `PolicyBundles` stores them per slot (`platform`, `company`) with `put`, `get` and `revoke`. |
| `runs.py` | `Runs`: the run record, attempt counts and delivered pull requests. |
| `participants.py` | `RunParticipants`: ephemeral agent rows, revocation, the spawn count, and `ParticipantRevoked`. |
| `sandboxes.py` | `SandboxRegistrations`: run to sandbox address. |
| `tickets.py` | `FiledTickets`: so the heartbeat reads its own tickets before the git host lists them. |
| `ledger.py` | `BudgetLedger`: spend rows and their sums. |
| `audit.py` | `AuditStore`: `append`, `story(run_id)`, `after` for followers, `count_allowed`, `latest_human_verdict`. |
| `memory.py` | In-memory doubles of every accessor except `PolicyBundles`, for other packages' unit tests. They fail the way the real accessors do. No running service imports it. |

### The nine tables

| Table | Written by | Records |
|---|---|---|
| `companies` | reconciler, console | The compiled spec (reconciler): not the blueprint's text, which stays on the Company CR. Who paused the heartbeat, and when (console). |
| `agent_instances` | reconciler | Standing agents, with their namespace. |
| `policy_bundles` | reconciler | Bundle tarball and revision, one row per slot. |
| `runs` | orchestrator | Task, status, artifacts, and the conclusion (`pr_url`, `verdict`). |
| `run_participants` | orchestrator | One row per ephemeral agent: its whole identity. |
| `sandbox_registrations` | mcp-gateway | Run to sandbox address. |
| `filed_tickets` | mcp-gateway | Each ticket the company filed. |
| `budget_ledger` | llm-gateway | One spend entry per model call. |
| `audit_events` | mcp-gateway, llm-gateway, egress proxy, orchestrator, console | Every decision, append only. |

## How it works

### Each table has one writer

Which accessor a service imports is its write authority. Postgres grants
don't enforce it, so review the imports. `companies` has one writer per
column set. A re-applied company stays paused.

`audit_events` has many writers and one accessor, told apart by `source`
(`gateway`, `llm-gateway`, `sandbox-infra`, `process-gate`,
`process-lifecycle`, `console`). Arguments are stored as `args_digest`.
The `rationale` column is the only cleartext the actor wrote.
`audit_events.run_id` and `sandbox_registrations.run_id` carry no
foreign key, so an event whose run claim failed verification is still
recorded.

### Every service migrates at startup

`open_registry()` applies `schema.sql` on every start, in any order,
concurrently. `migrate()` takes a transaction-scoped advisory lock
(`pg_advisory_xact_lock`). A session lock would survive a failed DDL on
a pooled connection and block every other service's migration. The
tables live in a `registry` schema in Temporal's Postgres.

`CREATE TABLE IF NOT EXISTS` leaves an existing table as it is. A new
column doesn't reach a database created before it. Add an idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, or recreate the cluster.

A call waits up to 30 seconds for a connection, then raises.

### Every write expects a retry

Temporal re-runs the orchestrator's activities after a worker dies, so
each write is safe to repeat:

- `Runs.start` upserts on `run_id`. A different run claiming a live
  issue or pull request fails on the partial unique indexes
  `runs_one_live_*_run`.
- `RunParticipants.add` upserts on the deterministic `agent_id`. Its
  `WHERE status = 'active'` never revives a terminated row: `add` raises
  `ParticipantRevoked`.
- `SandboxRegistrations.register` updates on conflict, since a provision
  retry brings a new address.
- `Runs.finish` recreates a missing row, so compensation never fails on
  it.

## Invariants

- Revocation is final:
  `test_registry.py::test_revocation_is_final_and_a_late_mint_cannot_revive_it`.
- Terminated rows read as absent:
  `test_participant_active_goes_dark_on_termination`,
  `test_terminated_registration_is_not_served`.
- A retried `start` lands on its own row: `test_start_is_retry_safe`.
- One live run per issue and per pull request:
  `test_one_live_implement_run_per_issue`,
  `test_one_live_review_run_per_pr`.
- `finish` recreates a missing row:
  `test_finish_recreates_a_missing_row`.
- An unattributable event is still recorded:
  `test_unattributable_attempts_are_still_recorded`.
- One query returns a run's story across every source:
  `test_story_spans_every_source`.
- A re-apply keeps the pause:
  `test_the_pause_is_a_company_fact_that_survives_a_re_apply`.
- Identical policy packs identically:
  `test_bundles.py::test_identical_files_pack_identically`.
- Bundle roots come from the files' `package` lines:
  `test_roots_come_from_the_package_declarations`.
- A tombstone must declare its roots:
  `test_a_rootless_tombstone_is_refused`.

## Testing

These tests need a real Postgres. Without `REGISTRY_DSN` every test
skips, `test_bundles.py` included, and the run is green. The autouse
`clean` fixture deletes every row of all nine tables after each test.
Point it at a throwaway database, never at a cluster you use:

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_USER=poseidon \
  -e POSTGRES_PASSWORD=ci -e POSTGRES_DB=poseidon postgres:16.6
REGISTRY_DSN=postgresql://poseidon:ci@127.0.0.1:5432/poseidon uv run pytest packages/registry
```

CI runs the same container. Run tests per package
([Contributing](../../docs/developing/contributing.md)). On the cluster,
`tests/e2e_kind/test_registry_durability.py` checks that a run's record
and sandbox registration survive a gateway restart.

## Related

- [Architecture](../../docs/explanation/architecture.md): who holds
  what, and writes where.
- [Policy and audit](../../docs/reference/policy.md): the audit events
  and their names.
- [`reconciler`](../reconciler/README.md),
  [`orchestrator`](../orchestrator/README.md),
  [`console`](../console/README.md),
  [`mcp-gateway`](../mcp-gateway/README.md),
  [`llm-gateway`](../llm-gateway/README.md),
  [`sandbox`](../sandbox/README.md).
