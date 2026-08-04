-- Registry schema (overview §4.1, plan step 8): the join point between
-- declarative intent and live state.
--
-- Lives as a schema inside the Postgres instance deployed for Temporal
-- (plan step 2) rather than a separate database, so no CREATE DATABASE has
-- to race with the services that use it. Every statement is IF NOT EXISTS:
-- services migrate at startup under an advisory lock, in any order, on
-- every restart.
--
-- Write ownership (enforced by which accessor a service imports, and by the
-- fact that each table has exactly one writer):
--   operator      -> companies, agent_instances, policy_bundles
--   orchestrator  -> runs, run_participants; crew_requests together with
--                    the gateway (§4.4 rule 3: the crew.request tool and
--                    the workflow path record the same row)
--   gateway       -> sandbox_registrations, audit_events
--   egress proxy  -> audit_events
--   LLM gateway   -> budget_ledger                       (plan step 14)

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
    hire          text NOT NULL,
    role_template text NOT NULL,
    model_tier    text,
    -- The company namespace the instance's pod (and ServiceAccount) live
    -- in. Workload identity (plan step 11) verifies a caller's projected
    -- token against this: same SA name in any other namespace is rejected.
    namespace     text,
    status        text NOT NULL DEFAULT 'pending',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- Databases migrated before plan step 11 predate the column.
ALTER TABLE registry.agent_instances ADD COLUMN IF NOT EXISTS namespace text;

-- Runs: replaces runs/<id>/manifest.json. The workflow id is the §5.5
-- mapping; artifacts (tickets, PR) are recorded against the run.
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

-- Ephemeral crew members, kept out of agent_instances so hire/fire diffs
-- stay clean (§4.4 rule 2). Revocation is a row termination, which is what
-- makes it effective before TTL expiry.
CREATE TABLE IF NOT EXISTS registry.run_participants (
    agent_id        text PRIMARY KEY,
    run_id          text NOT NULL REFERENCES registry.runs(run_id) ON DELETE CASCADE,
    parent_agent_id text NOT NULL,
    crew_template   text NOT NULL,
    role            text NOT NULL,
    status          text NOT NULL DEFAULT 'active',
    created_at      timestamptz NOT NULL DEFAULT now(),
    terminated_at   timestamptz
);
CREATE INDEX IF NOT EXISTS run_participants_run_id_idx
    ON registry.run_participants (run_id);

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
-- stays reconcilable (§4.8). Written by the LLM gateway at plan step 14.
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

-- Recorded crew requests: authorization + intent, never fulfillment
-- (§4.4 rule 3). Idempotent and expiring. Consumed at plan step 16.
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

-- Compiled policy bundles (§2b): the operator persists them here and the
-- bundle server hands them to OPA over the standard bundle API, which OPA
-- polls. Storing them makes gateway and OPA restarts safe — nothing has to
-- be re-pushed, and the revision says exactly which blueprint is live.
CREATE TABLE IF NOT EXISTS registry.policy_bundles (
    company    text PRIMARY KEY,
    revision   text NOT NULL,
    tarball    bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The audit store (sequencing choice 1: JSONL retires here), schema v2
-- since plan step 13 (§5.2): the v1 fields plus company/lifecycle/parent/
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
-- Databases migrated before plan step 13 predate the v2 columns.
ALTER TABLE registry.audit_events ADD COLUMN IF NOT EXISTS source          text NOT NULL DEFAULT 'gateway';
ALTER TABLE registry.audit_events ADD COLUMN IF NOT EXISTS company         text NOT NULL DEFAULT '';
ALTER TABLE registry.audit_events ADD COLUMN IF NOT EXISTS lifecycle       text NOT NULL DEFAULT '';
ALTER TABLE registry.audit_events ADD COLUMN IF NOT EXISTS parent_agent_id text NOT NULL DEFAULT '';
ALTER TABLE registry.audit_events ADD COLUMN IF NOT EXISTS workflow        text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS audit_events_run_id_ts_idx
    ON registry.audit_events (run_id, ts);
