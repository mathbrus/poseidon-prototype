# MVP direction: lean standing roster (crews deliver; standing monitors)

> **Status:** work in progress. Direction notes for review — not a frozen
> implementation plan. Cross-walked against
> `[company-blueprint.md](company-blueprint.md)`,
> `[standing-and-ephemeral-agents.md](standing-and-ephemeral-agents.md)`,
> `[durable-orchestrator.md](durable-orchestrator.md)`,
> `[flexible-coding-agents.md](flexible-coding-agents.md)`, and
> `[cleaned/mvp-overview.md](cleaned/mvp-overview.md)`.
>
> **Precedence.** This document was added after the other MVP building-block
> notes and the consolidated overview. Where it collides with them (roster
> shape, who executes implement/review, dual standing-vs-crew paths, the
> child-scope ⊆ parent rule, and related resolutions), **this document wins**
> until those docs are rewritten to match. Do not re-litigate lean roster by
> citing an older draft.

## Goal

Standing vs ephemeral answers *how long* an agentic construct lives. It does
not answer *which jobs deserve a standing brain*.

The current MVP drafts still hire a **standing Backend Engineer** and a
**standing SDET**, while also proving that the PM can spawn a
`feature-delivery` crew that already contains the coding executor. That is a
**redundant brain on top of a crew**: a long-lived roster member whose only
job is to do (or sit above) work the platform already models as an ephemeral
crew.

**Lean standing roster** is the building block that draws the line:

- **Standing agents** exist for continuous company judgment — intake,
  prioritization, deciding *when* to spawn work, and (later) monitoring /
  analysis that runs across many runs.
- **Ephemeral crews** exist for bounded SDLC work — implement a ticket, review
  a PR. The PM spawns them directly; there is no standing engineer or SDET in
  the middle.

Example shape:

```text
Standing PM (company roster)
  ├─ intake: scopes goals → tickets
  ├─ for implement run R: request feature-delivery crew
  │     ├─ planner (ephemeral)
  │     └─ executor (ephemeral; coding brain + sandbox)
  └─ for review run S: request feature-review crew
        └─ reviewer (ephemeral; read diff, run tests, post findings)

Standing analyst (placeholder — not built in MVP)
  └─ cross-run monitoring / signals (future)
```

This block owns **roster intent**: which role templates are standing at MVP,
which work is crew-only, and how workflow nodes bind to spawners rather than
to standing executors. It does not own spawn mechanics (standing/ephemeral),
durability (orchestrator), or brain quality (flexible agents).

## Not the same as standing vs ephemeral

`[standing-and-ephemeral-agents.md](standing-and-ephemeral-agents.md)` defines
the lifecycle axis and the request/fulfill spawn contract. This block
**narrows the standing side of that axis**.

| Concern | Standing vs ephemeral | Lean standing roster (this block) |
|---|---|---|
| Question | How long does a construct live? | Which jobs get a standing hire at all? |
| Schema | `standing` / `ephemeral`; crew templates; `may_spawn` | Which templates appear under `departments.roles` vs only under `crews` |
| Failure mode | Ephemeral leaks; operator creates run agents | Standing Backend Engineer / SDET duplicate crew work |

Keep both documents. Lifecycle without roster intent still leaves redundant
hires; roster intent without lifecycle has no spawn contract.

## What changes

Today / drafts-as-written:

- MVP company ≈ three standing roles: PM, Backend Engineer, SDET.
- `implement` may dispatch a standing engineer **or** a PM-spawned crew
  (both paths exercised).
- `review` dispatches a standing SDET.
- Crews are framed as *how a standing role delivers*, not as a substitute
  for the org chart of executors.

MVP under this block:

1. **Standing MVP roster is lean:** Product Manager only (plus optional
   placeholder roles that are declared but not implemented — see analyst
   below). No standing Backend Engineer. No standing SDET.
2. **`feature-delivery` is the implement path** — not an extension proof
   beside a standing engineer. The PM requests the crew; the orchestrator
   fulfills; the coding executor is ephemeral and reuses the coding role
   *template* / flexible brain without a standing hire.
3. **`feature-review` is the review path** — same spawn shape. The PM
   (or the review workflow on the PM's authority) requests a review crew;
   members post findings through gateway tools; the human still holds merge.
4. **Workflow nodes bind to standing spawners, not standing executors.**
   `implement` and `review` map to the PM (who holds `may_spawn` for the
   relevant crews). Coding and review *capability* lives in crew-member
   templates and compiled child scopes — not in standing `AgentInstance`s.
5. **Role templates ≠ standing hires.** `backend-engineer` (and a review
   role template) remain in the role catalog for ephemeral members and
   compile-time priors. Instantiating them as standing `count: N` is what
   this block forbids for MVP delivery/review.
6. **Drop child-scope ⊆ standing parent.** That rule assumed the standing
   parent was a capability supersystem (an engineer spawning helpers under
   their own tools). A conductor PM is not that. Authority to spawn a crew
   is not authority to personally wield every tool the crew uses — see
   [O5](#o5--child-tool-policy).

## What does not change

1. **Request/fulfill spawn** — still standing request + orchestrator
   fulfill; gateway allow is not fulfillment.
2. **Process state stays in the orchestrator** — crews do not own HITL merge
   gates; the PM does not become a second Temporal.
3. **Flexible coding brain** — still the executor implementation behind
   Hands → Law; still does not own company or crew lifecycle.
4. **Human holds merge** — review crew finds; orchestrator parks; human
   signals; orchestrator merges.
5. **No backwards compatibility** — drop the dual implement path; do not keep
   a standing engineer "just in case."
6. **Child scopes stay explicit and compiled** — each crew member still has
   an allow-list and a Rego projection; only the ⊆-parent compile check goes
   away.

## What the blueprint declares (roster shape)

Illustrative, not final schema:

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
            - crew: feature-review
              max_concurrent: 2
    # Future standing monitors — declared as placeholders only; count 0 /
    # disabled until the role has a real job description and tools.
    insights:
      roles:
        - template: analyst
          count: 0          # placeholder: not hired in MVP
          model_tier: mid
          # may_spawn: []   # monitoring crews TBD
  tools:
    pm:
      # Conductor scope only — not a union of every crew member's tools.
      allow: [issues.*, git.read, crew.request]
  crews:
    feature-delivery:
      members:
        - role: planner
          model_tier: mid
          allow: [issues.read, issues.write, git.read]
        - role: executor
          template: backend-engineer   # catalog template, not a standing hire
          model_tier: mid
          allow: [sandbox.*, git.open_pr]
      budget:
        inherit: implement
    feature-review:
      members:
        - role: reviewer
          template: sdet               # catalog template, not a standing hire
          model_tier: mid
          allow: [git.read, git.comment, sandbox.read_file, sandbox.exec, sandbox.run_tests]
      budget:
        inherit: review
  workflows:
    intake: { enabled: true }
    implement: { enabled: true }     # fulfilled via feature-delivery
    review:
      enabled: true
      gates: { merge: human_approval }  # fulfilled via feature-review
```

Compiler must:

- Reject standing hires of delivery/review templates that the company has
  chosen to run as crews only (or, simpler for MVP: the hello-saas catalog
  simply does not list them under `departments.roles`).
- Validate `may_spawn` → known crews; each crew member has an explicit
  `allow` (from the crew template and/or catalog role). Do **not** require
  child `allow` ⊆ standing parent `allow`.
- Map workflow nodes to standing roles that *spawn or conduct* that phase
  (PM for implement/review), not to ephemeral member roles.
- Still emit crew-child Rego projections and catalog-based brain bindings for
  ephemeral members.

### Why ⊆ parent is dropped

⊆ parent made sense when a standing engineer was the real capability holder
and ephemeral helpers were narrower delegates. Under lean roster the standing
parent is a **conductor**: the PM decides *whether* to spawn `feature-delivery`
or `feature-review`; the crew template decides *what tools* members get.

Keeping ⊆ parent would force the PM's declared allows to become the union of
planner + executor + reviewer scopes — a fiction that either:

- inflates the PM brain's legal tool surface (and then needs a second
  phase/policy layer to claw it back), or
- blocks lean roster entirely (coding crews cannot hang under a honest PM).

**New rule:** spawn authorization is `may_spawn` + crew template + budget /
concurrency caps. Tool authorization for ephemeral callers is the member's
compiled allow-list (and ordinary gateway/OPA args checks). Parentage still
appears on every decision for audit and budgets; it is not a capability
ceiling.

## Placeholder: standing analyst

MVP **does not implement** an analyst. The blueprint may reserve the template
and a department so the lean-roster story is honest about *future* standing
hires:

- Standing monitors (analyst, later SRE-like watchers, etc.) are the intended
  growth path for the roster — continuous observation across runs, budgets,
  quality signals, backlog health.
- They are **not** a rename of Backend Engineer or SDET. They do not execute
  `implement` or `review`.
- Until there is a concrete tool surface and spawn policy, `count: 0` (or
  omit the department entirely in the first hello-saas apply).

## Target architecture: thin roster, thick crews

```text
┌──────────────────────────────────────────────────────────┐
│  Intent — Company Blueprint                              │
│  standing: PM (+ analyst placeholder)                    │
│  crews: feature-delivery, feature-review                 │
└────────────────────────────┬─────────────────────────────┘
                             │ compile
┌────────────────────────────▼─────────────────────────────┐
│  Plan — AgentInstance CRs for standing only              │
│         CrewTemplate catalog + child Rego                │
└───────────────┬──────────────────────────┬───────────────┘
                │ reconcile                │ fulfill spawn
┌───────────────▼──────────────┐  ┌────────▼───────────────────────────┐
│  Reality — Standing roster   │  │  Process — Orchestrator            │
│  PM (stable agent_id)        │  │  intake / implement / review       │
│  analyst: not hired yet      │  │  feature-delivery / feature-review │
└───────────────┬──────────────┘  └────────┬───────────────────────────┘
                │                          │
                │         ┌────────────────▼────────────────┐
                └────────►│  Ephemeral crews (run_participants)│
                          │  planner / executor / reviewer     │
                          └────────────────┬────────────────┘
                                           │ Hands → Law
```

| Layer | Owns | Must not own |
|---|---|---|
| **This block (roster intent)** | Which jobs are standing vs crew-only; MVP hire list; placeholder monitors | Spawn fulfillment; Temporal; brain internals |
| **Standing / ephemeral** | Lifecycle classes; `may_spawn`; request/fulfill; `run_participants` | Deciding that Backend Engineer must be standing |
| **Blueprint / compiler** | Emitting only the standing CRs the lean roster requires | Secretly re-adding standing executors "for simpler exit criteria" |
| **Orchestrator** | Sequencing intake → crew implement → crew review → merge gate | Dispatching a standing engineer/SDET that was never hired |
| **Flexible coding agents** | Ephemeral executor (and later reviewer) brain quality | Requiring a standing engineer Deployment to host the brain |

## Relation to other MVP building blocks

| Building block | Relationship |
|---|---|
| **Company Blueprint** | Roster section shrinks; crew templates and catalog templates carry delivery/review capability — see `[company-blueprint.md](company-blueprint.md)` |
| **Standing and ephemeral** | Keeps the spawn contract; drops standing Backend Engineer / SDET, the "crews are not a substitute org chart" framing for executors, and **child `allow` ⊆ parent** (O3 = B there) — see `[standing-and-ephemeral-agents.md](standing-and-ephemeral-agents.md)` |
| **Durable orchestrator** | `implement` / `review` activities dispatch via crew fulfillment, not standing engineer/SDET HTTP — see `[durable-orchestrator.md](durable-orchestrator.md)` |
| **Flexible coding agents** | Brain binds to the ephemeral executor member (catalog template), not to a standing hire — see `[flexible-coding-agents.md](flexible-coding-agents.md)` |
| **Sandbox hardening** | Unchanged altitude; sandboxes still pair with ephemeral coding/review members — see `[sandbox-hardening.md](sandbox-hardening.md)` |
| **Consolidated overview** | Overturns the "both implement paths", "SDET is the standing reviewer", and child-scope ⊆ parent resolutions — see `[cleaned/mvp-overview.md](cleaned/mvp-overview.md)` |

**Altitude check:** Blueprint answers *what is the company?*; standing vs
ephemeral answers *how long constructs live and who may spawn whom?*; this
block answers *which brains are worth keeping warm on the roster?*; the
orchestrator answers *how do processes survive?*; flexible agents answer
*how does an executor think?*

## Hard problems this design must acknowledge

1. **Spawn is not a blank check.** Dropping ⊆ parent means `may_spawn` must
   stay load-bearing: which crews, concurrency, budgets, and compiled child
   scopes. A PM who may request `feature-delivery` still cannot personally
   call `git.open_pr` unless the PM allow-list says so.
2. **Workflow mapping rewrite.** Compiler checks that "every workflow node
   has a hired role" must mean *standing spawner / conductor*, not *standing
   executor*. Ephemeral members are not hires.
3. **Path ownership and catalog roles.** Policies that name
   `backend-engineer` as a path owner still make sense as *template / crew
   role* identity on ephemeral callers — not as a standing instance id.
4. **Review crew shape.** One reviewer member is enough for MVP; multi-role
   review crews wait. Sandbox-from-PR-ref vs read-only review stays an open
   cost call (overview O-4), now applied to the ephemeral reviewer.
5. **Intake stays standing.** Lean roster does not make the PM ephemeral;
   continuous judgment across goals is why the PM is standing.
6. **Analyst temptation.** Do not smuggle Backend Engineer or SDET back in
   under the analyst name. Analyst is monitor/insight, or it stays
   `count: 0`.

## Failure matrix (additions)

| Failure | MVP behavior |
|---|---|
| Blueprint still lists standing `backend-engineer` / `sdet` for hello-saas | Treat as doc/schema bug against this block; do not ship dual roster |
| Implement workflow dispatches a standing engineer identity | Fail closed — that instance must not exist |
| Review workflow expects a standing SDET | Same — spawn `feature-review` or fail |
| PM lacks `may_spawn` for the phase's crew | Compile or apply fails; no silent fallback to a standing executor |

## Phase placement

| Phase | Role of this block |
|---|---|
| **PoC** *(done)* | One hardcoded coding agent; no roster |
| **MVP** | Standing PM only; `feature-delivery` + `feature-review`; analyst placeholder only |
| **V1** | First real standing monitor (analyst or peer) with a concrete tool surface |
| **V2** | Richer monitor roster; still no return of standing executors as the default delivery path |

### Exit criteria (candidates)

- hello-saas applies with **one** standing `AgentInstance` (PM); no standing
  Backend Engineer or SDET CRs.
- Intake → PM tickets; implement → PM `crew.request` `feature-delivery` →
  planner + executor under `run_id` → PR via gateway; no standing engineer
  dispatch on the path.
- Review → PM (or workflow on PM authority) `crew.request` `feature-review`
  → findings via gateway → merge gate → human signal → orchestrator merge.
- Kill worker mid-crew: ephemeral identities and sandboxes torn down; standing
  PM remains.
- Deny path: unauthorized crew spawn denied; PM cannot call executor-only
  tools; executor cannot exceed its crew-member allow-list — gateway deny,
  not prompt hope.
- Analyst: template may exist; `count: 0` means no instance, no tools exercised.

## Practical split

| Concern | MVP direction |
|---|---|
| Standing hires | PM only |
| Standing placeholders | `analyst` at `count: 0` (optional) |
| Implement execution | Ephemeral `feature-delivery` only |
| Review execution | Ephemeral `feature-review` only |
| Catalog templates `backend-engineer` / `sdet` | Reused by crew members; not standing |
| Dual implement path (standing engineer **or** crew) | **Deleted** |
| Child tool policy | Explicit per crew member; **not** ⊆ standing parent |
| Merge authority | Human via orchestrator gate (unchanged) |

## Collisions (make these explicit)

### C1 — Redundant standing executors vs crews

Standing Backend Engineer / SDET duplicate the jobs of `feature-delivery` /
`feature-review`. Keeping both "for simpler exit criteria" reintroduces the
redundant brain.

**Split:** crews own delivery and review execution; standing owns continuous
judgment and spawn decisions.

### C2 — "Crews are not a substitute org chart"

Standing/ephemeral text says crews are *how* a standing role delivers, not a
substitute org chart, and keeps workflow nodes mapped to standing executors.
That framing blocks lean roster.

**Split:** crews **are** the org chart for task execution. Standing org chart
is monitors + conductors (PM, later analyst). Workflow nodes map to standing
*spawners*.

### C3 — Overview resolution "both implement paths"

`cleaned/mvp-overview.md` adopts standing engineer as the core implement exit
path and crew as an extension proof. This block overturns that.

**Split:** one implement path — PM-spawned `feature-delivery`.

### C4 — "SDET is the MVP reviewer"

Overview and durable-orchestrator name a standing SDET as reviewer.

**Split:** ephemeral `feature-review` reviewer (catalog template `sdet`);
human holds merge.

### C5 — Child scope ⊆ standing parent (standing/ephemeral O3 = B)

Older drafts require every child `allow` ⊆ the standing parent's allow-list.
That assumes the parent is a capability supersystem. Lean roster makes the
parent a conductor; ⊆ parent then forces PM scope inflation or blocks crews
under the PM. The "nest under standing engineer" fallback is also gone.

**Split:** drop ⊆ parent. Spawn gate = `may_spawn` + crew template; tool gate
= member allow-list (+ ordinary OPA arg checks). Parentage remains on the
decision input for audit/budgets, not as a capability ceiling.

## Choices / tradeoffs / options

### O1 — Who is standing at MVP?


| Option | Tradeoff |
|---|---|
| **A. PM + Backend Engineer + SDET** (current drafts) | Familiar SDLC org chart; redundant brains on crews |
| **B. PM only; delivery/review are crews** | Honest lean roster; requires dropping ⊆ parent; workflow mapping rewrite |
| **C. PM + standing SDET; delivery is crew-only** | Half-measure; review still duplicates a crew |

**Decision:** **B**.

### O2 — Dual implement path?


| Option | Tradeoff |
|---|---|
| **A. Keep both** (overview §11 row 3) | Simpler first exit; permanently confuses roster intent |
| **B. Crew-only implement** | One story; crew machinery on the critical path from day one |
| **C. Standing-engineer-only; crews later** | Delays the spawn proof this program already wants |

**Decision:** **B**. No dual path.

### O3 — Review crew parent?


| Option | Tradeoff |
|---|---|
| **A. PM spawns `feature-review`** | One standing spawner; consistent with delivery |
| **B. Orchestrator auto-spawns review crew without a standing parent** | Skips `may_spawn` / standing request story; weaker audit parentage |
| **C. Keep standing SDET as parent of a thinner review crew** | Brings the redundant brain back |

**Decision:** **A**.

### O4 — Analyst in MVP schema?


| Option | Tradeoff |
|---|---|
| **A. Omit entirely until V1** | Cleanest apply; weaker signal that standing ≠ only PM forever |
| **B. Placeholder template at `count: 0`** | Documents intent; zero runtime cost if compiler honors count |
| **C. Hire a no-op analyst** | Waste; invents a job description under pressure |

**Decision:** **B** (optional in hello-saas; required in the narrative of this
block).

### O5 — Child tool policy


| Option | Tradeoff |
|---|---|
| **A. Keep ⊆ standing parent** (standing/ephemeral O3 = B) | Least-privilege story for engineer-as-parent; forces PM allow-list to be a union of all crew tools under lean roster |
| **B. Explicit child allow-lists from crew/catalog only; no ⊆ parent** | Honest conductor PM; spawn auth and tool auth are separate; must not treat parentage as a capability ceiling |
| **C. Child may exceed parent only for named crew templates** | Half-measure; still encodes ⊆ as the default mental model |

**Decision:** **B**. Overturns standing/ephemeral O3 = B and overview compile
checks that require child scope ⊆ parent. `may_spawn` authorizes *which crew*;
the crew template authorizes *which tools* each member gets.

## Verdict

Invest in a **lean standing roster**: hire standing brains only for continuous
company judgment (PM now; analyst-shaped monitors later). Delete standing
Backend Engineer and SDET from the MVP company. **`feature-delivery` and
`feature-review` are the implement and review paths**, spawned directly by the
PM under the existing request/fulfill contract — not proofs beside redundant
standing executors.

Role catalog templates remain for ephemeral members and policy identity. Drop
**child `allow` ⊆ standing parent**: a conductor's spawn right is not a
superset of crew tools. The dual implement path, standing-SDET reviewer, and
⊆-parent compile rule in older drafts are overturned by this block. Do not
reintroduce standing executors as a shortcut around crew fulfillment.
