# registry

The join point between declarative intent and live state: nine Postgres
tables, each behind a write-scoped accessor class. The operator writes
what a blueprint declared, the orchestrator writes what a run is doing,
the gateways write what they decided — and one `run_id` query returns the
whole story across all of them.

It is a **shared library, not a service**. The writers are already
separate processes with separate identities; putting an HTTP hop in front
of Postgres would add a failure mode without adding a boundary. Each
service holds `REGISTRY_DSN`, constructs the accessors it is allowed to
write through, and talks to Postgres directly.

In [the root README](../../README.md)'s architecture diagrams this is the
`registry` cylinder: the declarative path ends here (what was applied) and
the runtime path reports here (what every enforcement point decided).

## The nine tables

| Table | Written by | Records |
|---|---|---|
| `companies` | operator | The applied blueprint spec, one row per company. |
| `agent_instances` | operator | The standing agents, including the namespace workload identity checks against. |
| `policy_bundles` | operator | The compiled OPA bundle tarball and its revision. |
| `runs` | orchestrator | The run record: task, status, artifacts, terminal state — and whether the run *concluded* its work item (`pr_url` for implement, `verdict` for review), which the heartbeat's attempt accounting reads. |
| `run_participants` | orchestrator | One row per bound ephemeral identity — the whole identity, ServiceAccount name included. Revocation is a row termination. |
| `sandbox_registrations` | gateway | Run → sandbox address. The registration *is* the run binding. |
| `filed_tickets` | gateway | Every ticket the company filed, by repository and number, from the moment the git host created it — the heartbeat reads its own writes from here while the host's listing catches up. |
| `budget_ledger` | LLM gateway | One spend entry per model call, fully attributed. |
| `audit_events` | gateway, LLM gateway, egress proxy, orchestrator | Every decision, one ingestion contract. |

Seven tables have exactly one writer. The discipline is not a Postgres
grant — every service connects with the same DSN — but a code convention
made auditable by the import graph: which accessor a service imports *is*
its write authority, and you can grep for it. It buys provenance without
coordination: a row's origin is knowable from its table alone, no writer
needs a protocol with another, and each accessor can encode exactly the
retry semantics its one writer needs.

The one exception is deliberate. `audit_events` inverts the pattern: many
writers, one accessor, append only. Four services write it, distinguished
by the `source` column, and what is single is the *contract*, not the
writer.

## Module map

| Module | Owns |
|---|---|
| `db.py` | Connection pool, DSN handling, and startup migration under an advisory lock. |
| `schema.sql` | The nine tables. Comment-only edits are safe; DDL changes are not — see the invariants. |
| `companies.py` | `Companies` — the applied blueprint per company, written by the operator. |
| `instances.py` | `AgentInstances` — the standing agents, and the gateways' workload-identity read. |
| `policy.py` | `PolicyBundles` — stored compiled bundles, plus deterministic bundle packing. |
| `runs.py` | `Runs` — the run record and its terminal state. |
| `participants.py` | `RunParticipants` — bound identities, revocation, the spawn count. |
| `sandboxes.py` | `SandboxRegistrations` — run → sandbox address. |
| `crews.py` | `CrewRequests` — idempotent, expiring spawn requests. |
| `ledger.py` | `BudgetLedger` — one ledger with dimensions, never parallel accounts. |
| `audit.py` | `AuditStore` — the one ingestion contract, and the `story(run_id)` query. |
| `memory.py` | In-memory stand-ins for every accessor. **Shipped on purpose** — see below. |

## Migration is every service's job

There is no migration job and no ordering requirement. Every service calls
`open_registry()` at startup, which applies `schema.sql` — every statement
is `IF NOT EXISTS`, so services migrate in any order, on every restart,
concurrently. Concurrency is real: `CREATE TABLE IF NOT
EXISTS` can still collide in Postgres, so `migrate()` serializes under one
advisory lock.

The lock is transaction-scoped (`pg_advisory_xact_lock`), not
session-scoped, and `db.py` explains why: a failing DDL aborts the
transaction, and a session lock taken inside it would survive the rollback
on a pooled connection while the unlock call cannot run — wedging
every other service's `migrate()` until the pool recycles.

The schema also lives *inside* the Postgres instance deployed for
Temporal, as a `registry` schema rather than a separate database, so no
`CREATE DATABASE` has to race the services that migrate at startup.

## Every write expects to be retried

The orchestrator's writes come from Temporal activities, which are
re-executed after crashes and worker deaths ([Temporal](../../docs/concepts/temporal.md)). So the registry's job is to make
"the retry lands on the same row" a property of the SQL, not a hope:

- `Runs.start` upserts — a re-attempt resets the same row (and clears a
  stale terminal state) instead of creating a second run; a *different*
  run claiming the same live issue or PR task dies on the one-live-run
  partial indexes instead, so a re-offered item can never be worked twice
  at once.
- `RunParticipants.add` upserts on the deterministic `agent_id` the mint
  activity derives from `{crew, role, run}` — a retried mint converges
  on one identity instead of leaking two.
- `CrewRequests` derives its id from `{conductor, crew_template, run_id}`
  so a retried request refreshes rather than duplicates; `consume` is an
  atomic `recorded → fulfilled` claim that hands a mid-fulfill crash's
  retry its own request back while giving any other run nothing.
- `SandboxRegistrations.register` updates on conflict, because a
  provision retry re-registers a new address for the same run.
- `Runs.finish` recreates a missing row rather than failing, because it
  runs on the compensation path, which must never be the thing that fails.

## Revocation is a row termination, not an expiry

`run_participants` is where the ephemeral-identity contract from the root
README becomes concrete:

```sql
CREATE TABLE IF NOT EXISTS registry.run_participants (
    agent_id        text PRIMARY KEY,
    run_id          text NOT NULL REFERENCES registry.runs(run_id) ON DELETE CASCADE,
    parent_agent_id text NOT NULL,
    crew_template   text NOT NULL,
    role            text NOT NULL,
    company         text NOT NULL DEFAULT '',
    namespace       text NOT NULL DEFAULT '',
    service_account text,
    status          text NOT NULL DEFAULT 'active',
    created_at      timestamptz NOT NULL DEFAULT now(),
    terminated_at   timestamptz
);
```

The gateway accepts an ephemeral agent's ServiceAccount token only while a
live (`active`, unterminated) row backs it — resolved by
`service_account` name, checked on every call, and read for everything
the identity is (company, catalog template, namespace, crew
coordinates). That makes revocation a database write: flip `status` to
`terminated` and the identity is dead on the next call — however long
its projected tokens remain valid. Token expiry alone
would leave a compromised identity usable for the remainder of its TTL;
a row check leaves it usable for zero further calls. Teardown uses the
sweep form, `terminate_for_run`, which revokes every live ephemeral agent of a
run at once and returns who it swept (each becomes an audit event).

Revocation is also **final**, and the mechanism is worth reading:

```sql
ON CONFLICT (agent_id) DO UPDATE SET ...
WHERE run_participants.status = 'active'
RETURNING agent_id
```

The `WHERE` on the upsert means a mint that hits a terminated row updates
nothing and returns no row — and `add` raises `ParticipantRevoked`. The
only caller that can reach a terminated row is a mint retry that lost the
race with its run's teardown, and reviving the row would resurrect an
identity the platform already revoked, with nothing left in the workflow
to collect the pod holding it. The in-memory double raises the same
exception, so unit suites exercise the same sharp edge.

## The audit store: one `run_id`, six sources

`audit_events` is the table the phrase "whatever happened, one query tells
the story" refers to. Six sources write it in practice — `gateway` (tool
decisions and rejected tokens), `llm-gateway` (model calls),
`sandbox-infra` (the egress proxy's allows and denies), `process-gate`
(gate verdicts and escalations), `process-lifecycle` (crew lifecycle) and
`console` (a human's acts, the only rows carrying a person's name) —
all through `AuditStore.append`, all in the shape of `common.audit`'s
`AuditEvent`. `story(run_id)` returns every event for a run ordered by
`(ts, id)`, backed by an index on `(run_id, ts)`. Because every
enforcement point records the same fields, one query reconstructs a run
across all of them. Two reads feed policy: `count_allowed` (a per-run
tool cap) and `latest_human_verdict` (the console-recorded verdict the
merge gate is released on).

Two shapes of restraint define the table. There is **no foreign key on
`run_id`**, on purpose: an event whose `run_id` is a *claim* that failed
verification — an unattributable egress attempt, a stale run binding — is
exactly the event worth keeping, and a foreign key would refuse it. And
arguments are stored as **digests** (`args_digest`), never raw: the audit
store must not become the leak channel the rest of the design closes.

## Policy bundles are content-addressed

`bundles.pack` builds the OPA bundle tarball the operator persists and the
bundle server hands to OPA. Two properties are load-bearing:

- **The revision is a digest of the content**, and the tar metadata
  (mtime, uid, ownership) is pinned, so identical policy produces an
  identical tarball. Re-applying an unchanged blueprint is a no-op for
  OPA; a changed one is unmissable.
- **The roots are derived from the files' own `package` declarations**,
  never hardcoded. OPA bundle activation erases everything under a
  bundle's roots that the bundle does not supply, so a root any wider than
  the contents would delete policy another bundle owns on the first poll.
  A file without a `package` declaration is refused outright.

`PolicyBundles.delete` is company teardown's policy revocation: the bundle
stops being served, so a caller that still holds an identity finds no rule
to match.

## `memory.py` is not test-only scaffolding to delete

The in-memory doubles are part of the package's public surface. The unit
suites of `gateway`, `llm-gateway`, `orchestrator` and `sandbox` import
them to run without a database. They live next to the real accessors
because a double that drifts from its interface is worse than no double,
and they mirror behaviour, not just signatures: `record_artifacts` on an
unknown run is a no-op (an `UPDATE` matching zero rows, not a `KeyError`),
and `InMemoryRunParticipants` raises `ParticipantRevoked` on a revived row
exactly as Postgres does. A double with friendlier failure modes than the
real thing hides exactly the bugs it exists to catch. Nothing in a running
service may import this module — the point of the registry is that state
survives the process.

## Entry points

A library only — no service, no CLI, no Dockerfile. Callers hold
`REGISTRY_DSN` (default `postgresql://poseidon@postgres:5432/poseidon`)
and call `open_registry()`, which opens the pool and migrates. Calls block
up to 30 s for a connection rather than degrading silently: run records
and audit events are not optional.

## Cross-package interactions

- [`common`](../common/README.md) supplies the `AuditEvent` shape — the
  registry's only dependency besides psycopg.
- [`operator`](../operator/README.md) writes `companies`,
  `agent_instances` and `policy_bundles` on reconcile; its bundle server
  reads `policy_bundles` for OPA to poll.
- [`orchestrator`](../orchestrator/README.md) writes `runs` and
  `run_participants`, emits the `process-gate` and `process-lifecycle`
  audit events, and reads the ledger for headroom checks and the spawn
  count off `run_participants` when it fulfils a crew.
- [`gateway`](../mcp-gateway/README.md) writes `sandbox_registrations`
  and audit events; reads `agent_instances` (workload identity),
  `run_participants` (ephemeral identity backing) and
  `sandbox_registrations` (run binding).
- [`llm-gateway`](../llm-gateway/README.md) writes `budget_ledger` and
  audit events; its hard stop compares `spent_for_run` before every call.
- [`sandbox`](../sandbox/README.md)'s egress proxy writes audit events
  and resolves a sandbox namespace back to its company's allowlist via
  `Companies.by_namespace`.

## Tests

```
REGISTRY_DSN=postgresql://poseidon:...@127.0.0.1:5432/poseidon uv run pytest packages/registry
```

**These tests skip without `REGISTRY_DSN`**, which is expected on a laptop
— `uv run pytest packages/registry` reports every test skipped, and that
is green.
The SQL is the thing under test — upsert convergence, the revocation
`WHERE` clause, the cross-source `story` query — so a fake would test
nothing. CI provides a Postgres service container; locally,
`kubectl -n poseidon-platform port-forward svc/postgres 5432:5432` against
the kind cluster works. (The pure bundle-packing tests in
`test_bundles.py` need no database at all; they skip anyway because the
suite-wide cleanup fixture depends on the database session fixture.)

As everywhere in this repo, run pytest per package, never repo-wide.

## Invariants

- **Revocation is final.** A terminated `run_participants` row is never
  revived. The only way to reach one is to mint after the run's teardown
  already swept it — a retry racing compensation — and that caller is
  told to stop rather than allowed to resurrect a revoked identity.
- **No foreign key on `audit_events.run_id`**, on purpose: an event whose
  `run_id` is a claim that failed verification is exactly the event worth
  keeping. `sandbox_registrations.run_id` has none either — coupling the
  gateway's write to another service's insert ordering would trade a real
  failure mode for referential tidiness.
- **Audit args are digests.** The audit store must not become a leak
  channel.
- **Terminated rows read as absent.** `SandboxRegistrations.active` and
  `RunParticipants.active` return `None` once terminated, which is what
  makes a stale `run_id` claim or a revoked token fail closed.
- `schema.sql` is applied by every service at startup. Changing DDL is a
  wire-compatibility question across all writers, not a local edit.
