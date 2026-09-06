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
-- one writer; the eighth is shared by design:
--   operator      -> companies, agent_instances, policy_bundles
--   orchestrator  -> runs, run_participants
--   gateway       -> sandbox_registrations
--   LLM gateway   -> budget_ledger
--   audit_events  <- gateway, LLM gateway, egress proxy, orchestrator
--                    (process-gate, process-lifecycle) and console: many
--                    writers, one append-only contract

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
    -- The hire's name in the blueprint's `standing:` section — also the
    -- agent's Deployment and Service name in the company namespace.
    standing_name text NOT NULL,
    -- The catalog role: what the agent is, and what policy keys on.
    role          text NOT NULL,
    -- The company namespace the agent's pod (and ServiceAccount) live
    -- in. Workload identity verifies a caller's projected token against
    -- this: same SA name in any other namespace is rejected.
    namespace     text,
    status        text NOT NULL DEFAULT 'pending',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Runs. The workflow id maps a run to its Temporal workflow; artifacts
-- (tickets, PR) are recorded against the run. Two columns say whether a
-- run *concluded* its work item, whatever its terminal status: an
-- implement run concluded when it delivered (pr_url), a review run when
-- the reviewer reached a verdict (verdict). A run that ended with neither
-- aborted, and the heartbeat may offer its item again — bounded by the
-- blueprint's attempt cap. pr_settled remembers what the world did with a
-- delivered PR once the heartbeat has seen it merged or closed on the git
-- host: observed once, never read again.
CREATE TABLE IF NOT EXISTS registry.runs (
    run_id      text PRIMARY KEY,
    workflow    text NOT NULL DEFAULT 'implement',
    workflow_id text,
    company     text,
    task        text NOT NULL DEFAULT '',
    status      text NOT NULL,
    pr_url      text,
    pr_settled  text,
    verdict     text,
    ticket_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    iterations  integer NOT NULL DEFAULT 0,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
-- One live implement run per issue. The canonical issue-run task is the
-- key and the constraint is structural: the heartbeat, a replayed
-- workflow and a human `poseidon run <n>` cannot race their way into two
-- live runs for the same ticket — the second insert dies here, not in a
-- check-then-act. Only running issue-shaped implement runs are
-- constrained; free-form task runs and finished runs are not.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_issue_run
    ON registry.runs (company, task)
    WHERE status = 'running' AND workflow = 'implement'
      AND task LIKE 'implement issue #%';  -- common.tasks.issue_run_task
-- One live review run per pull request, by the same construction: the
-- canonical review-run task is the key, so a heartbeat re-offering an
-- aborted review and a human `poseidon run` cannot review one PR twice
-- at once.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_review_run
    ON registry.runs (company, task)
    WHERE status = 'running' AND workflow = 'review'
      AND task LIKE 'review pull request #%';  -- common.tasks.review_run_task

-- Ephemeral agents, kept out of agent_instances so hire/fire diffs
-- stay clean. Revocation is a row termination, checked live on every
-- gateway call. The row is the whole identity: the gateway resolves an
-- ephemeral agent's projected ServiceAccount token to this row (by
-- service_account), and company/role/namespace are read from here —
-- there is no token claim to fall back on.
CREATE TABLE IF NOT EXISTS registry.run_participants (
    agent_id        text PRIMARY KEY,
    run_id          text NOT NULL REFERENCES registry.runs(run_id) ON DELETE CASCADE,
    parent_agent_id text NOT NULL,
    crew_template   text NOT NULL,
    -- The catalog role this agent is; with crew_template, what the
    -- compiled crew rules key on.
    role            text NOT NULL,
    company         text NOT NULL DEFAULT '',
    -- The company namespace the agent's pod and ServiceAccount live in.
    -- Workload identity verifies a caller's projected token against this:
    -- the same SA name in any other namespace is rejected.
    namespace       text NOT NULL DEFAULT '',
    -- RFC-1123 name of the agent's per-run ServiceAccount. The gateway
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
-- Every ticket the company files, recorded by the gateway the moment the
-- git host created it — the PM's tickets, the platform's bootstrap and
-- architecture-request tickets. The host lists a new issue up to a
-- minute after creating it; the heartbeat's observation reads its own
-- writes from here and completes the listing by number, so a ticket the
-- company just filed is never invisible to its next tick. Written by the
-- gateway only.
CREATE TABLE IF NOT EXISTS registry.filed_tickets (
    repo      text NOT NULL,
    number    integer NOT NULL,
    run_id    text NOT NULL,
    filed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, number)
);

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
    cached_tokens     integer NOT NULL DEFAULT 0,
    reasoning_tokens  integer NOT NULL DEFAULT 0,
    usd               numeric(16, 10) NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS budget_ledger_run_id_idx ON registry.budget_ledger (run_id);

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
