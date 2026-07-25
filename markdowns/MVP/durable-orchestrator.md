# MVP direction: durable orchestrator (PoC runner → workflow engine)

> **Status:** work in progress. Direction notes for review — not a frozen
> implementation plan. Cross-walked against
> `[company-blueprint.md](company-blueprint.md)`,
> `[sandbox-hardening.md](sandbox-hardening.md)`, and
> `[flexible-coding-agents.md](flexible-coding-agents.md)`; lineage from
> `[../poc/specification1.md](../poc/specification1.md)`,
> `[../poc/specification2.md](../poc/specification2.md)`, and
> `[../poc/prototype.md](../poc/prototype.md)`.

## Goal

The PoC proves the implement loop with a **plain CLI runner**: five named steps,
in-process timeouts, and teardown in a `finally` block. That is enough for
E1–E4. It is not enough once a feature can take days, wait on humans, span
multiple roles, or survive a worker crash.

**Durable orchestration** is the MVP building block that turns
`packages/runner` into a real **process-policy plane**: crash-resumable SDLC
workflows, human-in-the-loop gates, and compensation for sandboxes and partial
side effects — without making any one workflow-engine vendor the platform
foundation, and without moving cross-agent process state into agent brains.

**Temporal** is the *preferred candidate engine* for that plane (signals, timers, replayable history), not a peer product pillar equal to the Company Blueprint or the flexible coding brain. Any engine with durable execution, timers, and external signals can sit behind the same workflow/activity shape until a decision says otherwise.

## What Temporal is (context, not commitment)

Temporal (and peers in that class) provide:

- **Workflows** whose history survives process death and can resume.
- **Activities** for side-effecting work (HTTP calls, sandbox provision, agent
dispatch) with timeouts and retries.
- **Signals / updates** for human approval and external events.
- **Timers** for escalation when nobody approves.
- **Replayable history** useful for run forensics alongside the gateway audit
log.

Those properties collide with specific Poseidon components — see
[Collisions](#collisions-make-these-explicit) and
[Choices / tradeoffs / options](#choices--tradeoffs--options).

## What changes

Today (`packages/runner/`):

- A host CLI (`poseidon-poc`) plays orchestrator and supervisor.
- One hardcoded flow: `create_run` → `fetch_workspace` → `provision_sandbox` →
`dispatch_task` → `teardown`.
- Steps are named after future activities on purpose, but the process is a
script: if the runner dies mid-run, the run is lost (teardown only if the
process still reaches `finally`).
- No human-approval gates; process policy is absent from the PoC security
model.
- Run state is a local `runs/<run_id>/manifest.json`.

MVP must:

1. **Rename** `packages/runner` → `packages/orchestrator` (CLI becomes a thin
  client that starts/signals/queries workflows, not the durability substrate).
2. Own company-level SDLC as durable workflows: **intake → implement → review**
  in MVP (release / incident wait for V1).
3. Map the PoC implement steps onto activities; keep them thin — call existing
  gateway / `SandboxBackend` / agent HTTP clients; do not reinvent those planes
   inside workers.
4. Make **human-in-the-loop an orchestrator primitive**: workflows park on
  signals configured by blueprint `workflows.*.gates`; Studio/API later
   surfaces the inbox (API signal is enough for MVP).
5. Guarantee **compensation**: sandbox destroy (and other cleanup) runs even
  when the worker dies, the activity times out, or a human rejects.
6. Correlate workflow identity with `run_id` so gateway audit + gate outcomes
  tell one story.
7. Deploy the engine **self-hosted** on the same Kubernetes path as the rest of
  the platform (kind/k3d/minikube locally — no Temporal Cloud / managed
   dependency for MVP).



## What does not change

1. **Agents still hold no credentials.** Orchestrator workers must not become a
  second secret bag for `GIT_TOKEN` or provider keys. Side effects still leave
   through gateways.
2. **Semantic policy stays at the MCP Gateway + OPA.** Process policy answers
  “who approved / may this run continue?”; it cannot see “which branch does
   `git.open_pr` target?” Those layers are complementary.
3. **Blueprint remains the source of truth.** Workflow/gate config is a compiled
  projection of the blueprint; the engine must not become a second org/policy
   authority.
4. **Orchestrator owns provision/destroy.** Activities call `SandboxBackend`;
  backends (and coding frameworks) do not reconcile companies or invent their
   own workspace lifecycle — see `[sandbox-hardening.md](sandbox-hardening.md)`
   and `[flexible-coding-agents.md](flexible-coding-agents.md)`.
5. **LangGraph / coding brain stays inside the agent.** Cross-agent process
  state (who reviews what, what’s approved) never lives in the brain graph.
6. **Transport default for orchestrator→agent stays HTTP.** SLIM enters where
  group sessions earn it (review); do not put SLIM on every activity hop.



## Target architecture: process plane above law

```text
┌──────────────────────────────────────────────────────────┐
│  Intent — Company Blueprint                              │
│  workflows.* + gates → workflow/gate config              │
└────────────────────────────┬─────────────────────────────┘
                             │ compile (pure)
┌────────────────────────────▼─────────────────────────────┐
│  Plan — workflow artifacts                               │
│  (enabled processes, gate kinds, timeouts/escalation)    │
└────────────────────────────┬─────────────────────────────┘
                             │ start / signal / query
┌────────────────────────────▼─────────────────────────────┐
│  Process — Orchestrator (workflow engine + workers)      │
│    preferred: Temporal (self-hosted)                     │
│    peers: any durable engine with timers + signals       │
│    packages/orchestrator/  (was: packages/runner)        │
└───────────────┬──────────────────────────┬───────────────┘
                │ activities               │ activities
                ▼                          ▼
┌───────────────────────────┐  ┌───────────────────────────┐
│  Reality — agents /       │  │  Reality — SandboxBackend │
│  listeners + workers      │  │  provision / destroy      │
└───────────────┬───────────┘  └───────────────────────────┘
                │ tool calls
┌───────────────▼──────────────────────────────────────────┐
│  Law — MCP gateway + OPA + LLM gateway                   │
│  (+ process-gate outcomes in the same audit story)       │
└──────────────────────────────────────────────────────────┘
```


| Layer                    | Owns                                                                                  | Must not own                                                          |
| ------------------------ | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| **Blueprint / compiler** | Which workflows exist; which gates/signals; drain defaults                            | Calling the workflow API as source of truth; live credentials         |
| **Orchestrator**         | Durability, activity timeouts/retries, HITL waits, compensation, `run_id` correlation | Semantic branch/path rules; kernel sandbox enforcement; org reconcile |
| **Agent brain**          | Local reasoning loop for a task                                                       | Cross-agent approvals; sandbox lifecycle                              |
| **MCP Gateway + OPA**    | AuthN/Z, semantic allow/deny, audit of tool calls                                     | Being the HITL inbox or crash-resume substrate                        |


Suggested package shape:

```text
packages/orchestrator/          # was: packages/runner
  cli/                          # start / signal / status (thin)
  workflows/                    # intake, implement, review (+ later release, incident)
  activities/                   # create_run, fetch_workspace, provision_sandbox,
                                # dispatch_task, teardown, … (thin clients)
  gates/                        # signal handlers + timeout/escalation helpers
  worker/                       # process that hosts workflows + activities
```



### MVP workflow set


| Workflow      | What it does (MVP depth)                                                              |
| ------------- | ------------------------------------------------------------------------------------- |
| **intake**    | Goal → PM scopes → tickets via gateway → budget reserved                              |
| **implement** | Ticket → engineer → sandbox → iterate → PR via gateway (PoC five steps as activities) |
| **review**    | PR → reviewer + SDET (SLIM group when ready) → findings → **gate: merge policy**      |


Release and incident remain V1.

### Implement activity map (from PoC)


| PoC step            | Orchestrator activity                                                       |
| ------------------- | --------------------------------------------------------------------------- |
| `create_run`        | Mint/correlate `run_id`; persist run record                                 |
| `fetch_workspace`   | Gateway `repo.archive`                                                      |
| `provision_sandbox` | `SandboxBackend.provision` + gateway sandbox register                       |
| `dispatch_task`     | Agent HTTP dispatch + wait for terminal status                              |
| `teardown`          | `SandboxBackend.destroy` + finalize run record (also on compensation paths) |




## Defense in depth (three layers — process fills the gap)


| Layer              | Question                               | PoC                                         | Durable MVP                               |
| ------------------ | -------------------------------------- | ------------------------------------------- | ----------------------------------------- |
| **Infrastructure** | What can the process physically touch? | Docker networks, credential-free containers | Hardened sandbox backends + NetworkPolicy |
| **Semantic**       | What does this action mean?            | MCP gateway + OPA                           | Same; blueprint-compiled per-role bundles |
| **Process**        | Who approved / may this continue?      | *(missing)*                                 | Workflow gates / HITL signals             |


A bypass of one layer must still be caught by another.

## Relation to the other MVP building blocks


| Building block             | Relationship to durable orchestrator                                                                                       |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Company Blueprint**      | `workflows.*.gates` configures which signals exist and default drain behavior; orchestrator owns durability, not org truth |
| **Sandbox hardening**      | Activities call `SandboxBackend`; process gates stay *above* infra policy                                                  |
| **Flexible coding agents** | Brain runs *inside* `dispatch_task`-class activities; must not own HITL or sandbox lifecycle                               |
| **LLM budgets**            | Gateway hard-stops; orchestrator reacts (pause + escalate) — not silent degrade                                            |
| **SLIM review**            | Review workflow is the first load-bearing SLIM use; implement stays HTTP activities                                        |
| **Operator / Kubernetes**  | Operator materializes the company; orchestrator runs processes *against* it — two different control loops                  |


**Altitude check:** Blueprint answers *what is the company?*; flexible agents
answer *how does the engineer think?*; sandbox hardening answers *which runtime
isolates code?*; this block answers *how do long-running company processes
survive, wait on humans, and clean up?*

## Failure matrix (MVP — keep it small)

Temporal (or peer) gives the machinery; the design work is enumerating failures.
For MVP, only the load-bearing cases — not an exhaustive catalog:


| Failure                            | MVP compensation                                                       |
| ---------------------------------- | ---------------------------------------------------------------------- |
| Worker / process dies mid-activity | Resume from history; sandbox must not leak (teardown compensation)     |
| Sandbox dies mid-`implement`       | Fail the run cleanly + teardown; no silent re-provision loops in MVP   |
| Agent timeout / never finishes     | Mark run failed; teardown; finalize manifest/record                    |
| Human never approves merge         | Timer → escalate per blueprint gate config (API-visible pending state) |
| Budget hard-stop mid-run           | Pause workflow + escalate (LLM gateway signals orchestrator)           |


Deferred to V1+ (document, don’t build yet): review deadlocks, reorg-while-in-flight
drain edge cases beyond blueprint defaults, snapshot-and-park, close-abandoned-PR
polish, partial `git.open_pr` crash recovery beyond basic idempotency.

## Phase placement


| Phase            | Orchestration role                                                                                                           |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **PoC** *(done)* | Script runner; step names are the seam                                                                                       |
| **MVP**          | Self-hosted engine; `packages/orchestrator`; intake → implement → review; HITL on merge; crash-resume + sandbox compensation |
| **V1**           | release + incident; richer drain/reassign; Studio approval inbox                                                             |
| **V2**           | Earned autonomy mutates gate config via controlled overlays — still not model self-authorization                             |




### Exit criteria (candidates)

- Kill the orchestrator worker mid-`implement`, restart it: the workflow
**resumes or compensates** — no leaked sandbox, run record terminal and
honest.
- From a blueprint-enabled company: run **intake → implement → review**, park on
the merge gate, **approve via API signal**, and observe the gate outcome in
the same audit/`run_id` story as gateway decisions.
- Teardown (or equivalent compensation) runs on success, failure, and cancel.



## Practical split


| Concern                 | MVP direction                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------- |
| Package name            | `packages/orchestrator` (rename from `runner`)                                                                 |
| Engine                  | Temporal preferred; swappable if something fits better — not a hard dependency                                 |
| Hosting                 | **Self-hosted only** on platform Kubernetes (incl. local kind/k3d/minikube); no cloud offering as a dependency |
| Workflow set            | intake + implement + review                                                                                    |
| HITL surface            | API signal first; Studio UI later                                                                              |
| Activity bodies         | Thin clients over gateway / sandbox / agent — no new chokepoints                                               |
| PoC CLI behavior        | Become `poseidon` (or similar) start/signal/status over the engine                                             |
| Process state in agents | Forbidden — stays in the orchestrator                                                                          |
| Failure matrix          | Only the five rows above for MVP                                                                               |




## Collisions (make these explicit)

These are the places this block fights other Poseidon pieces or fights itself.
None are all resolved here — they are the review surface.

### C1 — Two control loops: Operator vs Orchestrator

The Company Operator reconciles *desired company shape*. The orchestrator runs
*processes against that shape*. If workers start creating AgentInstances,
rewriting policy bundles, or “fixing” drift, they become a second reconciler.

**Split:** operator owns structure; orchestrator owns runs. Workers read
registry/gateway endpoints; they do not apply blueprints.

### C2 — Secrets in workers

Activities need to call the gateway (runner/orchestrator token today) and
possibly cluster APIs for `SandboxBackend`. That is a privileged plane.
Stuffing `GIT_TOKEN` or LLM keys into the worker “for convenience” collapses
the PoC credential invariant.

### C3 — Brain frameworks that want to own the loop

OpenHands-class agents often assume they own session lifecycle, retries, and
sometimes sandbox start/stop. Poseidon activities must *dispatch into* the
brain and wait — the workflow owns timeout, cancel, and teardown.

### C4 — HITL vs semantic policy (approval token)

Spec 1 sketched approvals that the gateway/OPA can see (e.g. merge/deploy only
when the run carries an approval artifact). MVP could instead treat “workflow
reached allowed state” as enough and keep OPA unaware of HITL.

**This is an open review point** — see [O6](#o6--do-process-approvals-appear-in-opa).

### C5 — Run identity split-brain

PoC `run_id`, workflow id, and (later) ticket/PR ids must correlate. Without an
explicit mapping, audit reconstruction (E4’s promise at scale) breaks.

### C6 — Engine lock-in vs “preferred candidate”

If activity code imports Temporal APIs deeply in every call site, swapping the
engine becomes a rewrite. The PoC’s step-shaped functions are the seam — keep
a thin adapter so Temporal stays preferred, not sacred.

### C7 — Self-hosted ops vs “nothing young on the critical path”

Self-hosted Temporal (or peer) is operational surface on the MVP critical path.
That is accepted here (no cloud), but the engine version must be pinable in
Helm and runnable on kind — same discipline as other platform deps.

### C8 — Review needs SLIM; implement must not wait on it

Shipping intake → implement → review in one MVP slice risks blocking implement
durability on SLIM group-session maturity. Keep review’s group transport behind
the App SDK seam; implement activities stay on HTTP.

## Choices / tradeoffs / options

Explicit forks for review. Defaults below incorporate decisions already taken;
remaining items are **proposals**.

### O1 — Engine commitment


| Option                                                       | Tradeoff                                             |
| ------------------------------------------------------------ | ---------------------------------------------------- |
| **A. Hard-depend on Temporal**                               | Fastest path; lock-in                                |
| **B. Temporal preferred; keep activity/workflow seams thin** | Slight adapter cost; exit ramp if a peer fits better |
| **C. Build a mini orchestrator**                             | Avoids a dep; rebuilds durability poorly             |


**Decision:** **B**. Not a hard dependency — open to a better-fitting tool, but
do not invent one.

### O2 — MVP workflow breadth


| Option                                  | Tradeoff                                |
| --------------------------------------- | --------------------------------------- |
| **A. Only harden** `implement`          | Smallest slice; delays multi-role proof |
| **B. Ship intake → implement → review** | Matches MVP exit story; more surface    |
| **C. All five SDLC workflows**          | Too wide for MVP                        |


**Decision:** **B**.

### O3 — Hosting


| Option                                                       | Tradeoff                                                         |
| ------------------------------------------------------------ | ---------------------------------------------------------------- |
| **A. Temporal Cloud / managed**                              | Less ops; external dependency; weaker on-prem story              |
| **B. Self-hosted on platform Kubernetes**                    | Ops cost; full local+prod parity with blueprint’s K8s-only model |
| **C. Dev-only embedded substitute, Cloud in “real” deploys** | Dual-stack drift                                                 |


**Decision:** **B**. No cloud dependency; must run on our side (incl. local
cluster).

### O4 — Package / CLI rename


| Option                                      | Tradeoff                               |
| ------------------------------------------- | -------------------------------------- |
| **A. Keep** `packages/runner` **name**      | Honest to PoC; under-sells the role    |
| **B. Rename to** `packages/orchestrator`    | Matches specs; CLI becomes thin client |
| **C. Split: runner CLI + orchestrator lib** | Extra packages for little gain at MVP  |


**Decision:** **B**.

### O5 — How much failure matrix in MVP?


| Option                                                                                          | Tradeoff                      |
| ----------------------------------------------------------------------------------------------- | ----------------------------- |
| **A. Exhaustive compensation catalog**                                                          | Correct long-term; stalls MVP |
| **B. Only load-bearing rows** (crash, sandbox death, agent timeout, HITL timeout, budget pause) | Simple; some gaps documented  |
| **C. Happy path + crash-resume only**                                                           | Too thin for HITL/budgets     |


**Decision:** **B** — see [Failure matrix](#failure-matrix-mvp--keep-it-small).

### O6 — Do process approvals appear in OPA?


| Option                                                                              | Tradeoff                                              |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **A. Workflow state alone** (“gate passed” → next activity calls gateway)           | Simple; OPA cannot independently prove approval       |
| **B. Emit an approval artifact** (token/claim) that OPA requires on sensitive tools | Stronger defense in depth; design + threat model work |
| **C. Dual-write gate outcomes to audit only; OPA later**                            | Pragmatic interim; risk of forever-interim            |


**Status:** **open — needs review.** Do not hard-wire A or B in the first
workflow sketches without an explicit decision. Related collision: [C4](#c4--hitl-vs-semantic-policy-approval-token).

### O7 — Where do workers run?


| Option                                                                              | Tradeoff                                             |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------- |
| **A. Platform namespace only**                                                      | Simple RBAC; multi-tenant blast radius if mis-scoped |
| **B. Per-company worker pools**                                                     | Stronger isolation; more ops                         |
| **C. Platform workers + per-company agent workers** (hybrid, matches spec 2 agents) | Natural fit; clarify which secrets live where        |


**Proposal:** **C** — engine + orchestrator workers in platform ns; agent
reasoning work remains in company namespaces as today/spec2 hybrid.

### O8 — Sequencing vs Blueprint / Sandbox / Flexible agents


| Option                                                                   | Tradeoff                                        |
| ------------------------------------------------------------------------ | ----------------------------------------------- |
| **A. Blueprint gate config before HITL wiring**                          | Avoids hardcoding merge policy                  |
| **B. Durable** `implement` **first on hardcoded gates, blueprint later** | Faster durability demo; rework risk             |
| **C. Flexible brain before orchestrator**                                | Longer tasks without crash-resume/compensation  |
| **D. Parallel: durable implement track + blueprint gates**               | Good if implement activities stay gate-agnostic |


**Proposal:** **D** with **A** before claiming the full intake→review exit
criterion. Do not block crash-resume work on OpenShell or OpenHands.

## Verdict (provisional)

Invest in a **durable orchestrator** as the process-policy plane: rename
`packages/runner` → `packages/orchestrator`, run a **self-hosted** workflow
engine (Temporal preferred, not mandatory), and ship **intake → implement →
review** with HITL on merge and a small, honest compensation set.

Keep activity bodies thin over existing gateway / sandbox / agent seams. Keep
org truth in the blueprint, semantic law in MCP+OPA, and isolation in
`SandboxBackend`. Do not let the engine, the operator, or a coding framework
become a second control plane for the others’ concerns.

Resolve [O6](#o6--do-process-approvals-appear-in-opa) before merge/deploy gates
hard-wire an approval model. Resolve remaining collisions by picking from
[Choices / tradeoffs / options](#choices--tradeoffs--options) where still open.