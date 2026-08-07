# registry

The join point between declarative intent and live state: nine Postgres
tables and one write-scoped accessor per table. The operator writes what a
blueprint declared, the orchestrator writes what a run is doing, the
gateways write what they decided — and one `run_id` query returns the whole
story across all of them.

It is a **shared library, not a service**. The writers are already separate
processes with separate identities; putting an HTTP hop in front of Postgres
would add a failure mode without adding a boundary.

## Write ownership

Each table has exactly one writer, enforced by which accessor a service
imports:

| Writer | Tables |
|---|---|
| operator | `companies`, `agent_instances`, `policy_bundles` |
| orchestrator | `runs`, `run_participants`; `crew_requests` jointly with the gateway |
| MCP gateway | `sandbox_registrations`, `audit_events` |
| egress proxy | `audit_events` |
| LLM gateway | `budget_ledger` |

`crew_requests` is the one shared table, and deliberately so: whether the PM
calls the `crew.request` tool (gateway records) or a workflow records under
the PM's conductor authority (orchestrator records), the same row lands and
the same compiled policy was faced.

## Module map

| Module | Owns |
|---|---|
| `db.py` | Connection pool, DSN handling, and startup migration under an advisory lock. Every statement is `IF NOT EXISTS`, so services migrate in any order on every restart. |
| `schema.sql` | The nine tables. Comment-only edits are safe; DDL changes are not — see below. |
| `bundles.py` | `Companies`, `AgentInstances`, `PolicyBundles` — the operator's writes and the reads everyone else does against them. |
| `runs.py` | `Runs` — the run record and its terminal state. |
| `participants.py` | `RunParticipants` — one row per minted ephemeral identity. Revocation is a row termination. |
| `sandboxes.py` | `SandboxRegistrations` — run → sandbox address. The registration *is* the run binding. |
| `crews.py` | `CrewRequests` — recorded spawn authorization and intent, idempotent and expiring. |
| `ledger.py` | `BudgetLedger` — one ledger with dimensions, never parallel accounts. |
| `audit.py` | `AuditStore` — the one ingestion contract, and the `story(run_id)` query. |
| `memory.py` | In-memory stand-ins for every accessor. **Shipped on purpose** — see below. |

## `memory.py` is not test-only scaffolding to delete

The in-memory doubles are part of the package's public surface. Other
packages' unit suites (gateway, orchestrator, operator, gateway-llm) import
them to run without a database. They mirror the real accessors' behaviour
including the sharp edges — `InMemoryRunParticipants` raises
`ParticipantRevoked` on a revived row exactly as Postgres does — which is
what makes those suites meaningful rather than decorative.

## Entry points

A library only — no service, no CLI, no Dockerfile. Callers hold
`REGISTRY_DSN` and construct the accessors they are allowed to write
through.

## Tests

```
REGISTRY_DSN=postgresql://poseidon:...@127.0.0.1:5432/poseidon uv run pytest packages/registry
```

**These tests skip without `REGISTRY_DSN`**, which is expected on a laptop —
`uv run pytest packages/registry` reports 28 skipped and that is green. They
test SQL, so they run against a real Postgres; a fake would test nothing. CI
provides one as a service container, and the kind cluster provides one
locally if you want to run them for real.

## Invariants

- **Revocation is final.** A terminated `run_participants` row is never
  revived. The only way to reach one is to mint after the run's teardown
  already swept it — a retry racing compensation — and that caller is told
  to stop rather than allowed to resurrect a revoked identity.
- **No foreign key on `audit_events.run_id`**, on purpose: an event whose
  `run_id` is a *claim* that failed verification — an unattributable egress
  attempt, a stale run binding — is exactly the event worth keeping.
- **Audit args are digests.** The audit store must not become a leak channel.
- The registry schema lives *inside* the Postgres instance deployed for
  Temporal, in its default database, so no `CREATE DATABASE` has to race the
  services that migrate at startup.
- `schema.sql` is applied by every service at startup. Changing DDL is a
  wire-compatibility question across all writers, not a local edit.
