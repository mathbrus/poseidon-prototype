# MVP direction: standing and ephemeral agents (extends Company Blueprint)

> **Status:** work in progress. Direction notes for review — not a frozen
> implementation plan. Cross-walked against
> `[company-blueprint.md](company-blueprint.md)`,
> `[durable-orchestrator.md](durable-orchestrator.md)`,
> `[flexible-coding-agents.md](flexible-coding-agents.md)`, and
> `[sandbox-hardening.md](sandbox-hardening.md)`; lineage from
> `[../poc/specification1.md](../poc/specification1.md)`,
> `[../poc/specification2.md](../poc/specification2.md)`, and
> `[../poc/prototype.md](../poc/prototype.md)`.

## Goal

The Company Blueprint answers *what the company is*: departments, roles, tools,
workflows, policies, budgets — compiled into `Company` / `AgentInstance`
reality. That is necessary and not sufficient.

MVP must also answer the **lifecycle** question:

> Which agentic constructs **belong to the company** (survive reorgs, hold
> stable identity, may hold SLIM subscriptions), and which exist only **for a
> task** (spawned, collaborate, then disappear)?

**Standing and ephemeral agents** is the building block that extends the
blueprint with two lifecycle classes:

- **Standing agents** — long-lived roster members declared by the blueprint
  (e.g. the Product Manager). Hire/fire/reorg are blueprint diffs; the Company
  Operator converges them.
- **Ephemeral agents** — short-lived, task-oriented constructs spawned under a
  standing agent’s authority for one run (e.g. a planner and an executor the PM
  invokes to deliver scoped work). They are **not** hire/fire events and must
  not become permanent registry citizens by accident.

Example shape:

```text
Standing PM (company roster)
  └─ for run R: request task crew
       ├─ planner (ephemeral)
       └─ executor (ephemeral)
            └─ flexible coding brain + sandbox as needed
```

This block owns **lifecycle semantics and spawn policy**. It does not own
process durability (orchestrator), brain quality (flexible agents), or
isolation (sandbox).

## Not the same as listener vs worker

Spec 2’s hybrid **listener vs on-demand worker** is a *compute density* choice
for a known identity: a thin always-on process holds SLIM subscriptions; heavy
reasoning runs as Jobs that scale to zero.

This block is about *semantic kinds* of agentic constructs:

| Concern | Standing vs ephemeral (this block) | Listener vs worker (Spec 2) |
|---|---|---|
| Question | How long does this construct *exist* as an org/task participant? | How is compute packed for a known identity? |
| Identity | Standing = stable `agent_id`; ephemeral = run-scoped participant | Same identity; different pod shape |
| Blueprint | Standing roles + crew templates | Runtime deployment detail of an instance |

A standing engineer can still scale to zero between tasks. An ephemeral planner
still needs a real identity for the duration of the run even if it never holds
a SLIM subscription. Do not treat “Job pod” as “ephemeral agent” without
identity, policy, and teardown.

## What changes

Today / blueprint-as-written:

- Org ≈ static map or `count: N` role instances.
- Dispatch ≈ call one agent HTTP endpoint for a task.
- Implicit assumption: one hired runtime ≈ one brain doing the whole job.

MVP must:

1. Introduce **two lifecycle classes** in the platform vocabulary:
   - `standing` — blueprint-instantiated; operator-reconciled; registry row with
     stable `agent_id`.
   - `ephemeral` — run-scoped; orchestrator-fulfilled after a governed request;
     recorded as a run participant with parentage; torn down with the task (or
     on compensation).
2. **Extend the blueprint** (compiled projection, not a second source of truth):
   - Standing roles as today (`departments.roles`).
   - **Crew templates** — which ephemeral roles may be spawned, max concurrency,
     tool/model overlays, child allow-lists.
   - Per standing role: `may_spawn` — which crews that role may request.
3. Make **spawn a governed action**, not an agent free-for-all:
   - Standing agent **requests** a crew for `run_id` (tool / gateway call).
   - Orchestrator **fulfills**: mints ephemeral identities, installs run-scoped
     policy projections, dispatches work, tears down.
4. Keep a **clear ownership split**:
   - Operator: standing roster only.
   - Orchestrator: ephemeral spawn / wait / teardown / compensation (same
     altitude as sandbox lifecycle).
5. **Attribute every call**: tool calls from ephemeral agents carry
   `{company, standing_agent_id, ephemeral_agent_id, role, run_id}` so OPA,
   audit, and budgets tell one story.

## What does not change

1. **Agents still hold no credentials** — standing or ephemeral.
2. **Side effects still leave only through gateways** — crews do not get a
   private channel.
3. **Blueprint remains the source of truth** — crew templates, spawn caps, and
   child tool scopes are compiled projections; the orchestrator must not invent
   org policy.
4. **Process state stays in the orchestrator** — a PM does not become a second
   Temporal; a planner does not own HITL merge gates.
5. **Ephemeral agents are never Directory / OASF citizens** — same V2 deferral
   as instance publication in Spec 1.
6. **Law stays outside the model** — “PM may spawn planner+executor” is
   Rego/gateway (+ orchestrator fulfillment), not prompt text.
7. **PoC enforcement shape stays** — compiled bundles still target
   `{agent_id, role, run_id, tool, args}` (extended with parentage fields as
   needed); no second policy engine for “crew mode.”

## What the blueprint declares (extension)

Standing surface stays as in `[company-blueprint.md](company-blueprint.md)`.
Add operative schema for crews (illustrative, not final):

```yaml
apiVersion: poseidon/v1alpha1
kind: CompanyBlueprint
metadata:
  name: hello-saas
spec:
  departments:
    product:
      roles:
        - template: pm
          count: 1
          model_tier: mid
          may_spawn:
            - crew: feature-delivery
              max_concurrent: 2
    engineering:
      roles:
        - template: backend-engineer
          count: 1
          model_tier: mid
          # no may_spawn in MVP unless blueprint adds it
  crews:
    feature-delivery:
      members:
        - role: planner
          model_tier: mid
          allow: [issues.read, issues.write, git.read]
        - role: executor
          template: backend-engineer   # reuse coding role priors
          model_tier: mid
          allow: [sandbox.*, git.open_pr]
      budget:
        inherit: implement             # charge parent run / workflow budget
```

Compiler must:

- Validate every `may_spawn.crew` references a known crew template.
- Enforce **child `allow` ⊆ standing parent `allow`** (see [O3](#o3--child-policy)).
- Ensure budgets always attach to `run_id` (and standing parent), never orphan
  ephemeral spend.
- Keep workflow nodes mapped to **standing** roles; crews are *how* a standing
  role delivers, not a substitute org chart.
- Emit crew-template artifacts + per-child Rego projections as pure compile
  output (operator still only reconciles standing `AgentInstance` CRs).

## Target architecture: roster vs run participants

```text
┌──────────────────────────────────────────────────────────┐
│  Intent — Company Blueprint                              │
│  standing roles + crew templates + may_spawn + scopes    │
└────────────────────────────┬─────────────────────────────┘
                             │ compile (pure, diffable)
┌────────────────────────────▼─────────────────────────────┐
│  Plan — artifacts                                        │
│  • AgentInstance CRs (standing only)                     │
│  • CrewTemplate catalog entries                          │
│  • per-role + per-crew-child Rego / budget projections   │
└───────────────┬──────────────────────────┬───────────────┘
                │ reconcile                │ start run / fulfill spawn
┌───────────────▼──────────────┐  ┌────────▼───────────────────────────┐
│  Reality — Standing roster   │  │  Process — Orchestrator            │
│  Operator + Registry         │  │  durable workflow; spawn/teardown  │
│  (stable agent_id, optional  │  │  ephemeral identities for run_id   │
│   listener)                  │  └────────┬───────────────────────────┘
└───────────────┬──────────────┘           │ dispatch
                │                    ┌─────▼──────────────────────────┐
                │                    │  Ephemeral crew                │
                │                    │  planner / executor / …        │
                └────────────────────►  same Hands → Law path         │
                                     └─────┬──────────────────────────┘
                                           │ tool calls
                               ┌───────────▼──────────────────────────┐
                               │  Law — MCP + OPA + LLM gateways      │
                               └──────────────────────────────────────┘
```

| Layer | Owns | Must not own |
|---|---|---|
| **Blueprint / compiler** | Standing roles; crew templates; `may_spawn`; child scopes ⊆ parent; spawn caps | Live spawn/teardown; credentials |
| **Operator / standing registry** | Convergence of standing instances | Creating ephemeral agents as if they were hires |
| **Run participants store** | Ephemeral rows keyed by `run_id` + parentage; TTL / terminal status | Being the org chart or hire/fire diff surface |
| **Orchestrator** | Fulfilling spawn requests; wait; cancel; compensation; parent↔child correlation | Becoming org source of truth; creating standing `AgentInstance`s |
| **Standing agent brain** | Deciding *when* to request a crew and how to use results (within tools) | Secretly forking ungoverned runtimes; fulfilling its own spawn |
| **Ephemeral brain** | Local task reasoning (planner vs coding executor, etc.) | Cross-run memory as company truth; HITL; sandbox lifecycle |
| **Gateways** | Allow/deny with full parentage + run context | Parsing “crew” from free-form prompts |

## Spawn contract (MVP)

**Decision:** standing agent **requests**; orchestrator **fulfills** after policy
check ([O2](#o2--spawn-authority)).

1. Standing agent calls a governed tool (e.g. `crew.request`) with
   `{crew, run_id, reason, …}`.
2. MCP gateway + OPA check: caller is standing, `may_spawn` includes that crew,
   concurrency caps, budget headroom.
3. Gateway does **not** mint runtimes. It records an allowed request (or returns
   a capability the orchestrator already expects) and the durable workflow
   proceeds to spawn activities.
4. Orchestrator activities:
   - create `run_participants` rows for each crew member;
   - mint short-lived workload identity / JWT;
   - install run-scoped policy projection for each child role;
   - dispatch planner → executor(s) (fixed graph for MVP — see hard problems);
   - on terminal state: revoke identity, drop participants, teardown paired
     sandboxes.
5. Standing agent consumes results through ordinary tools / task status — it
   never holds child credentials or bypasses Hands → Law.

## Lifecycle rules (MVP-hard)

1. **Standing** created/updated/deleted only via blueprint apply + operator.
2. **Ephemeral** created only with
   `{run_id, parent_agent_id, crew_template, reason}`; destroyed on success,
   failure, cancel, timeout, or worker compensation — same seriousness as
   sandbox teardown.
3. **No promotion by default.** An ephemeral agent does not become standing
   because it was useful; that requires a blueprint diff.
4. **Identity is real but scoped.** Ephemeral gets a workload identity for
   gateway calls; revocation on teardown is mandatory.
5. **Memory.** Standing may gain company-scoped memory later. Ephemeral memory
   dies with the run unless explicitly written through governed tools (tickets,
   PR, docs).
6. **SLIM.** Standing listeners hold subscriptions when needed. Ephemeral
   default = HTTP dispatch only; group-session invites wait for V1 unless review
   already forces otherwise.
7. **Child policy.** Explicit child `allow` lists; each method must be ⊆ the
   standing parent’s allow-list ([O3](#o3--child-policy)).

## Registry representation

**Decision:** standing instances stay in the instance / roster table; ephemeral
agents live in a separate **`run_participants`** store ([O4](#o4--registry-representation)).

Hire/fire diffs stay meaningful: compiling a blueprint against the registry
must not treat in-flight planners as “extra hires” or require drain of every
Job pod as a reorg. Participants are joinable by `run_id` for audit and
budget; they are not Directory citizens and are not CR-reconciled as
`AgentInstance`.

## Relation to other MVP building blocks

| Building block | Relationship |
|---|---|
| **Company Blueprint** | This *extends* blueprint schema + compiler projections; standing roster remains the primary CR surface — see `[company-blueprint.md](company-blueprint.md)` |
| **Durable orchestrator** | Owns ephemeral spawn/teardown activities; must not let standing brains become the durability plane. Expands collision C1: workers must not create standing instances; they **may** fulfill ephemeral spawn under crew templates — see `[durable-orchestrator.md](durable-orchestrator.md)` |
| **Flexible coding agents** | Typical *executor* brain; planner may be a thinner graph. Framework still must not own company or crew lifecycle — see `[flexible-coding-agents.md](flexible-coding-agents.md)` |
| **Sandbox hardening** | Ephemeral coding members usually pair with a sandbox for that run; sandbox still orchestrator-owned — see `[sandbox-hardening.md](sandbox-hardening.md)` |
| **LLM budgets** | Meter by `run_id` + standing parent + ephemeral child; hard-stop still signals orchestrator |
| **SLIM review** | Standing listeners first; ephemeral stays off SLIM by default in MVP |

**Altitude check:** Blueprint answers *what is the company?*; this block answers
*which constructs are standing vs ephemeral, who may request whom, and how
law/budget/identity attach for the short-lived ones?*; the orchestrator answers
*how do those processes survive and clean up?*; flexible agents answer *how does
an executor think?*; sandbox hardening answers *which runtime isolates code?*

## Hard problems this design must acknowledge

1. **Request vs fulfill split must stay honest.** If the standing agent can
   somehow start child runtimes without an orchestrator activity, compensation
   and audit break. The gateway allow on `crew.request` is not fulfillment.
2. **Fan-out / cost amplification.** One standing PM × N crews × M executors can
   burn the monthly budget quickly. `max_concurrent` + per-run budgets are
   load-bearing, not polish.
3. **Reorg vs in-flight crews.** Blueprint drain policy must cover “standing
   parent fired/reassigned while crew is running” (abort / reassign parent /
   finish) — declare defaults per workflow; don’t improvise in the operator.
4. **Crew-internal coordination.** MVP: orchestrator sequences a small fixed
   graph (planner → one executor). Defer free-form multi-agent chat among
   ephemeral members unless SLIM review already needs it.
5. **⊆ parent can feel tight.** A PM’s standing allow-list must be wide enough
   that legitimate child scopes (e.g. executor `sandbox.*`) are still subsets —
   or the PM is the wrong parent for coding crews. Prefer widening the standing
   parent’s declared tools (still gateway-enforced) over breaking ⊆.
6. **Confusion with listener/worker hybrid.** Document and review against Spec 2
   so implementers don’t collapse the two axes.

## Failure matrix (MVP — keep it small)

| Failure | MVP compensation |
|---|---|
| Worker dies mid-crew | Resume or compensate; revoke ephemeral JWTs; destroy participants + sandboxes |
| Ephemeral agent timeout | Mark participant failed; teardown that member (and crew policy as configured); finalize run honestly |
| `crew.request` allowed but fulfill never runs | Request must be idempotent / expire; no orphan identities |
| Standing parent drained mid-crew | Follow blueprint drain default (abort crew for MVP unless finish is declared) |
| Budget hard-stop mid-crew | Pause workflow + escalate; do not spawn further members |

## Phase placement

| Phase | Role of this block |
|---|---|
| **PoC** *(done)* | One hardcoded agent; no standing/ephemeral distinction |
| **MVP** | Blueprint crew templates + `may_spawn`; request/fulfill spawn; `run_participants`; first crew = PM → planner + one coding executor on `implement` |
| **V1** | Richer crew graphs; optional SLIM for ephemeral group work; drain/reassign polish |
| **V2** | Promotion paths only via blueprint; Directory still ignores ephemeral instances |

### Exit criteria (candidates)

- From a blueprint with one standing PM and a `feature-delivery` crew: start a
  run; PM requests the crew; observe ephemeral planner + executor participants
  under `run_id`; both call the gateway with correct parentage; both are gone
  after teardown.
- Kill the worker mid-crew: compensation destroys ephemeral identities **and**
  sandboxes; no orphan pods / JWTs / participant rows left “running.”
- Deny path: standing engineer cannot request a crew the blueprint did not
  authorize; planner cannot call `git.open_pr` if child scope forbids it —
  gateway deny, not prompt hope.
- Child scope that is not ⊆ parent fails **compile** (or apply preview), not at
  first tool call.
- Reorg preview: firing the PM while a crew runs follows the declared drain
  default.

## Practical split

| Concern | MVP direction |
|---|---|
| Product / schema vocabulary | `standing` / `ephemeral` |
| Standing materialization | Blueprint → `AgentInstance` CRs → Company Operator |
| Ephemeral materialization | `crew.request` → orchestrator fulfill → `run_participants` |
| Child tool policy | Explicit `allow`; must be ⊆ standing parent |
| First concrete crew | PM → planner + one coding executor on `implement` |
| Crew coordination | Orchestrator-fixed graph (planner then executor) |
| Ephemeral transport | HTTP dispatch; no SLIM by default |
| Directory / OASF | Standing templates only later; ephemeral never published |

## Collisions (make these explicit)

### C1 — Operator vs Orchestrator (expanded)

Operator owns standing structure. Orchestrator owns runs **and** ephemeral
fulfillment. If workers create standing `AgentInstance`s, rewrite policy
bundles, or “fix” roster drift, they become a second reconciler.

**Split:** operator owns roster; orchestrator owns run participants. Fulfill
activities mint participants + identities; they do not apply blueprints.

### C2 — Agent-requested spawn without fulfillment

A standing brain that treats `crew.request` as “I already have children” will
race the workflow, leak sandboxes, and break compensation.

**Split:** request is authorization + intent; fulfill is orchestrator-only.

### C3 — Standing allow-list vs useful child scopes

⊆ parent forces honest parent scopes. If the PM cannot legally hold
`sandbox.*` even as an unused allow, coding crews cannot hang under the PM
without a schema lie.

**Mitigation:** declare standing parent allows that cover children; enforcement
still denies tools the PM’s *brain* should not use if phase/policy further
restricts — or nest coding crews under a standing engineer for implement.
Review this when the first crew is wired.

### C4 — `run_participants` vs audit join

If gateway audit only stores ephemeral `agent_id` without `run_id` +
`parent_agent_id`, E4-scale reconstruction fails.

**Split:** parentage is mandatory on the decision input shape for ephemeral
callers.

### C5 — Flexible brain wants its own sub-agents

OpenHands-class frameworks may spawn internal helpers. Those are **not**
Poseidon ephemeral agents unless they go through request/fulfill and gateway
identity. Unmediated framework sub-agents are out of policy.

### C6 — Budget attribution split-brain

Charging only the standing parent, only the child, or only the workflow without
a single `run_id` ledger creates unreconcilable spend.

**Split:** one run ledger; dimensions for parent + participant.

## Choices / tradeoffs / options

### O1 — Naming


| Option | Tradeoff |
|---|---|
| **A. Standing / ephemeral** | Clear lifecycle axis; slightly abstract |
| **B. Roster / crew** | Friendly product language; weaker schema enums |
| **C. Persistent role / task agent** | Familiar; “persistent” confuses with Temporal durability |

**Decision:** **A**. Schema and platform vocabulary use `standing` /
`ephemeral`. “Crew” remains fine as the *template* name for a set of ephemeral
members (`crews.feature-delivery`), not as the lifecycle enum.

### O2 — Spawn authority


| Option | Tradeoff |
|---|---|
| **A. Orchestrator-only expansion of workflows** | Safest; standing brains cannot choose when to collaborate |
| **B. Standing agent tool starts children directly** | Flexible; easy to bypass compensation |
| **C. Agent request + orchestrator fulfill** | Flexible *and* auditable; two-step contract to keep honest |

**Decision:** **C**.

### O3 — Child policy


| Option | Tradeoff |
|---|---|
| **A. Always inherit parent allow-list** | Simple; weak least privilege |
| **B. Explicit child allow-list, must be ⊆ parent** | Least privilege with a clear compile rule |
| **C. Explicit child allow-list, may differ if blueprint says so** | More power; more review surface |

**Decision:** **B**.

### O4 — Registry representation


| Option | Tradeoff |
|---|---|
| **A. Same instance table with** `lifecycle=ephemeral` **+ TTL** | One table; pollutes hire/fire diffs |
| **B. Separate** `run_participants` **table; standing table stays clean** | Clear altitude; join by `run_id` for audit |
| **C. No durable participant rows; logs only** | Too weak for revoke/compensation |

**Decision:** **B**.

### O5 — Sequencing vs Blueprint / Orchestrator / Flexible agents


| Option | Tradeoff |
|---|---|
| **A. Standing roster only; crews later** | Delays multi-agent delivery proof |
| **B. Schema + spawn contract with orchestrator early; first crew = PM → planner + one coding executor on** `implement` | Proves the axis without boiling the ocean |
| **C. Wait for flexible coding brain before any crew** | Better executors; delays lifecycle contract |
| **D. Full multi-crew fan-out in MVP** | Cost and coordination blow-up |

**Decision:** **B** (proposal adopted). Land crew templates + request/fulfill +
`run_participants` alongside durable `implement`; first concrete crew is PM →
planner + one coding executor. Do not block the spawn contract on OpenHands or
OpenShell maturity — a thinner executor brain is enough to prove governance.

## Verdict

Invest in an explicit **standing vs ephemeral** model as an extension of the
Company Blueprint. The blueprint still declares the company; this block declares
**how long agentic constructs live, who may request collaborators, and how
law/budget/identity attach for the short-lived ones**.

Standing roster stays operator-reconciled. Ephemeral crews are
**request-then-fulfill**: agents ask, the orchestrator mints `run_participants`
and tears them down with the same seriousness as sandboxes. Child tool scopes
are explicit and ⊆ parent. Hire/fire diffs stay clean because ephemeral rows
never live in the standing instance table.

First MVP proof: a standing PM requests `feature-delivery`; a planner and one
coding executor run under `run_id`; gateway parentage and teardown hold under
crash. Do not let framework-native sub-agents, Job pods, or worker-side
“helpfulness” become a second org chart.
