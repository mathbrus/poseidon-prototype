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
-- one writer, companies has one per column set, and audit_events is shared:
--   reconciler    -> companies (the spec), agent_instances, policy_bundles
--   console       -> companies (the pause columns)
--   orchestrator  -> runs, run_participants
--   gateway       -> sandbox_registrations, filed_tickets
--   LLM gateway   -> budget_ledger
--   audit_events  <- gateway, LLM gateway, egress proxy, orchestrator
--                    (process-gate, process-lifecycle) and console: many
--                    writers, append-only while its company lives
--
-- The one deleter of every table is the reconciler's teardown sweep
-- (`registry.sweep`): a deleted company's rows go, all of them, in one
-- transaction. policy_bundles is the exception: the company slot keeps
-- the tombstone, a revocation rather than a record of the company.

CREATE SCHEMA IF NOT EXISTS registry;

-- Standing structure, written by the reconciler at blueprint apply.
CREATE TABLE IF NOT EXISTS registry.companies (
    name          text PRIMARY KEY,
    -- The compiled spec the reconciler stores from the Company CR: what
    -- the platform enforces and reads. Not the blueprint a human wrote,
    -- whose text the CR keeps (`spec.blueprintSource`).
    compiled_spec jsonb,
    -- The pause: who holds the heartbeat paused and since when, written by the console
    -- under a human's act and read by the heartbeat on every tick. NULL
    -- means the loop may start work. The reason lives on the act's audit
    -- row, like every rationale.
    paused_by     text,
    paused_at     timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
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
-- (tickets, PR) are recorded against the run. Whether a run *concluded*
-- its work item is read off its columns, whatever its terminal status: an
-- implement run concluded when it delivered (pr_url) and its delivery was
-- not refused (pr_settled), a review run when the reviewer reached a
-- verdict (verdict). A run that ended short of that aborted, or delivered
-- something that was refused, and the heartbeat may offer its item again —
-- bounded by the blueprint's attempt cap.
CREATE TABLE IF NOT EXISTS registry.runs (
    run_id      text PRIMARY KEY,
    workflow    text NOT NULL DEFAULT 'implement',
    workflow_id text,
    company     text,
    task        text NOT NULL DEFAULT '',
    status      text NOT NULL,
    -- Whether the running run holds one of the company's
    -- max_concurrent_runs places. A run parked on a person's merge verdict
    -- gives its place back with its crew and sandbox (Runs.vacate); one
    -- whose pull request edits the contract tree keeps it.
    occupies    boolean NOT NULL DEFAULT true,
    pr_url      text,
    -- What became of the delivered PR once the platform learned it
    -- (common.workflows.PR_SETTLED): merged, refused (closed without
    -- merging, which returns the ticket) or superseded by a rework ticket.
    -- Learned once, never read from the git host again.
    pr_settled  text,
    -- Why a refused delivery was refused, in the platform's words: the
    -- retry's task carries it.
    pr_settled_reason text,
    verdict     text,
    ticket_urls jsonb NOT NULL DEFAULT '[]'::jsonb,
    iterations  integer NOT NULL DEFAULT 0,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
-- One live implement run per issue. The canonical issue-run task is the
-- key and the constraint is structural: the heartbeat, a replayed
-- workflow and a human `poseidon start implement <n>` cannot race their way into two
-- live runs for the same ticket — the second insert dies here, not in a
-- check-then-act. Only running implement runs are constrained, and every
-- implement run's task is a ticket's; finished runs are not.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_issue_run
    ON registry.runs (company, task)
    WHERE status = 'running' AND workflow = 'implement'
      AND task LIKE 'implement issue #%';  -- common.tasks.issue_run_task
-- One live review run per pull request, by the same construction: the
-- canonical review-run task is the key, so a heartbeat re-offering an
-- aborted review and a human `poseidon start review` cannot review one PR twice
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

-- Compiled policy bundles: the reconciler persists them here and the
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
-- `rule` names the rule policy answered with; `rationale` is the actor's
-- own stated reason, cleartext and bounded, on the rows that committed
-- the company to something — the one field that is not a digest.
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
    rationale       text,
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
-- The follower's read: a company's rows above a cursor, in id order.
CREATE INDEX IF NOT EXISTS audit_events_company_id_idx
    ON registry.audit_events (company, id);
