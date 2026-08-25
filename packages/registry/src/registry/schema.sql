-- Registry schema: the join point between declarative intent and live
-- state.
--
-- Lives as a schema inside the Postgres instance deployed for Temporal
-- rather than a separate database, so no CREATE DATABASE has
-- to race with the services that use it. Every statement is IF NOT EXISTS:
-- services migrate at startup under an advisory lock, in any order, on
-- every restart.
--
-- Write ownership (an import-graph convention, enforced by which accessor
-- a service imports — not by Postgres grants). Seven tables have exactly
-- one writer; the other two are shared by design:
--   operator      -> companies, agent_instances, policy_bundles
--   orchestrator  -> runs, run_participants
--   gateway       -> sandbox_registrations
--   LLM gateway   -> budget_ledger
--   crew_requests <- orchestrator together with the gateway (the
--                    crew.request tool and the workflow path record the
--                    same row)
--   audit_events  <- gateway, LLM gateway, egress proxy and orchestrator
--                    (process-gate, process-lifecycle): many writers, one
--                    append-only contract

CREATE SCHEMA IF NOT EXISTS registry;

-- Standing structure, written by the operator at blueprint apply.
CREATE TABLE IF NOT EXISTS registry.companies (
    name        text PRIMARY KEY,
    blueprint   jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS registry.agent_instances (
    agent_id      text PRIMARY KEY,
    company       text NOT NULL,
    standing_role          text NOT NULL,
    role_template text NOT NULL,
    model_tier    text,
    -- The company namespace the instance's pod (and ServiceAccount) live
    -- in. Workload identity verifies a caller's projected token against
    -- this: same SA name in any other namespace is rejected.
    namespace     text,
    status        text NOT NULL DEFAULT 'pending',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Runs. The workflow id maps a run to its Temporal workflow; artifacts
-- (tickets, PR) are recorded against the run.
CREATE TABLE IF NOT EXISTS registry.runs (
    run_id      text PRIMARY KEY,
    workflow    text NOT NULL DEFAULT 'implement',
    workflow_id text,
    company     text,
    task        text NOT NULL DEFAULT '',
    status      text NOT NULL,
    pr_url      text,
    ticket_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    iterations  integer NOT NULL DEFAULT 0,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
-- One live implement run per issue. The canonical issue-run task is the
-- key and the constraint is structural: the conduct heartbeat, a replayed
-- workflow and a human `poseidon run <n>` cannot race their way into two
-- live runs for the same ticket — the second insert dies here, not in a
-- check-then-act. Only running issue-shaped implement runs are
-- constrained; free-form task runs and finished runs are not.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_issue_run
    ON registry.runs (company, task)
    WHERE status = 'running' AND workflow = 'implement'
      AND task LIKE 'implement issue #%';

-- Ephemeral crew members, kept out of agent_instances so hire/fire diffs
-- stay clean. Revocation is a row termination, checked live on every
-- gateway call. The row is the whole identity: the gateway resolves a
-- crew member's projected ServiceAccount token to this row (by
-- service_account), and company/role_template/namespace are read from
-- here — there is no token claim to fall back on.
CREATE TABLE IF NOT EXISTS registry.run_participants (
    agent_id        text PRIMARY KEY,
    run_id          text NOT NULL REFERENCES registry.runs(run_id) ON DELETE CASCADE,
    parent_agent_id text NOT NULL,
    crew_template   text NOT NULL,
    role            text NOT NULL,
    company         text NOT NULL DEFAULT '',
    role_template   text NOT NULL DEFAULT '',
    -- The company namespace the member's pod and ServiceAccount live in.
    -- Workload identity verifies a caller's projected token against this:
    -- the same SA name in any other namespace is rejected.
    namespace       text NOT NULL DEFAULT '',
    -- RFC-1123 name of the member's per-run ServiceAccount. The gateway
    -- looks the row up by this name; lowercasing makes the SA name
    -- non-invertible, so the mapping lives here rather than in arithmetic.
    service_account text,
    status          text NOT NULL DEFAULT 'active',
    created_at      timestamptz NOT NULL DEFAULT now(),
    terminated_at   timestamptz
);
CREATE INDEX IF NOT EXISTS run_participants_run_id_idx
    ON registry.run_participants (run_id);
CREATE UNIQUE INDEX IF NOT EXISTS run_participants_service_account_idx
    ON registry.run_participants (service_account);

-- Run -> sandbox address. Deliberately no foreign key: the gateway writes
-- this row when the orchestrator registers a sandbox, and coupling that
-- write to another service's insert ordering would trade a real failure
-- mode for referential tidiness.
CREATE TABLE IF NOT EXISTS registry.sandbox_registrations (
    run_id        text PRIMARY KEY,
    url           text NOT NULL,
    namespace     text,
    job_name      text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    terminated_at timestamptz
);

-- One ledger with dimensions, never parallel accounts, so crew fan-out
-- stays reconcilable. Written by the LLM gateway.
CREATE TABLE IF NOT EXISTS registry.budget_ledger (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    company           text,
    run_id            text,
    agent_id          text,
    parent_agent_id   text,
    role              text,
    model             text,
    prompt_tokens     integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    usd               numeric(12, 6) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS budget_ledger_run_id_idx ON registry.budget_ledger (run_id);

-- Recorded crew requests: authorization + intent, never fulfillment.
-- Idempotent and expiring; the orchestrator consumes them when it
-- fulfills a crew.
CREATE TABLE IF NOT EXISTS registry.crew_requests (
    id            text PRIMARY KEY,
    run_id        text,
    conductor     text NOT NULL,
    crew_template text NOT NULL,
    reason        text,
    status        text NOT NULL DEFAULT 'recorded',
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz,
    fulfilled_at  timestamptz
);

-- Compiled policy bundles: the operator persists them here and the
-- bundle server hands them to OPA over the standard bundle API, which OPA
-- polls. Storing them makes gateway and OPA restarts safe — nothing has to
-- be re-pushed, and the revision says exactly which blueprint is live.
CREATE TABLE IF NOT EXISTS registry.policy_bundles (
    company    text PRIMARY KEY,
    revision   text NOT NULL,
    tarball    bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The audit store: the decision fields plus company/lifecycle/parent/
-- workflow and an explicit source — one ingestion contract for every
-- enforcement point. No foreign key on run_id on purpose: an event whose
-- run_id is a *claim* that failed verification — an unattributable egress
-- attempt, a stale run binding — is exactly the event worth keeping.
CREATE TABLE IF NOT EXISTS registry.audit_events (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL,
    run_id          text NOT NULL DEFAULT '',
    agent_id        text NOT NULL,
    role            text NOT NULL,
    tool            text NOT NULL,
    args_digest     text NOT NULL,
    decision        text NOT NULL,
    rule            text,
    latency_ms      integer NOT NULL DEFAULT 0,
    backend_status  text,
    source          text NOT NULL DEFAULT 'gateway',
    company         text NOT NULL DEFAULT '',
    lifecycle       text NOT NULL DEFAULT '',
    parent_agent_id text NOT NULL DEFAULT '',
    workflow        text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_events_run_id_ts_idx
    ON registry.audit_events (run_id, ts);
