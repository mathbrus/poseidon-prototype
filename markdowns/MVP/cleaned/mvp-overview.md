# Poseidon MVP — consolidated design overview

**Status:** consolidated draft. This document reconciles the five MVP direction
notes (`company-blueprint.md`, `durable-orchestrator.md`,
`flexible-coding-agents.md`, `sandbox-hardening.md`,
`standing-and-ephemeral-agents.md`) with each other and with the PoC code as it
actually exists. It is the single coherent statement of what the MVP stage is
and what its building blocks are. The individual building-block documents will
be rewritten from this one.

Everything under `markdowns/poc/` and the current `README.md` is **historical
context only** — useful lineage, no longer binding. Where this document
contradicts them, this document wins.

**How decisions are marked.** Every consequential choice below carries one of
three tags:

- **[decided]** — already settled in the draft docs; carried over unchanged.
- **[adopted]** — the drafts were open, ambiguous, or contradictory; this
  document resolves the point and records why. These are proposals with a
  default: they stand unless explicitly overturned in review.
- **[open]** — genuinely needs a human decision before implementation
  hard-wires anything. Collected in [§12](#12-open-decisions).

A platform-wide stance that shapes several resolutions below: **no backwards
compatibility, ever.** No dual deploy stacks, no migration shims, no frozen
bridges kept alive "just in case." When the MVP replaces a PoC mechanism, the
PoC mechanism is deleted.

---

## 1. Where we start: what the PoC actually delivered

The PoC proved, with running code and scripted exit criteria (E1–E4), that one
coding agent can ship one real PR **under enforcement rather than etiquette**:

- **Two invariants held by construction:** agents hold no credentials (git
  token only in the gateway, provider key only in the LLM proxy), and there is
  no unmediated egress (internal Docker networks; the sandbox has zero egress).
- **One chokepoint:** every side effect — including the agent's own sandbox
  file/exec calls — flows through the MCP gateway pipeline: authenticate
  (static bearer token) → OPA policy decision → append audit event → execute
  with platform-held credentials.
- **Semantic policy on real arguments:** `git.open_pr` is decided with the
  branch name *and the changed file paths* (the gateway applies the agent's
  `git bundle` to a platform-side clone before asking OPA), so rules like
  "deny PRs touching `.github/workflows/**`" are enforceable.
- **A survivable prompt injection:** the red-team task gets its `main`
  delivery denied by rule, audited, and the agent recovers onto `feature/*`.

Equally important, the PoC left the **seams** the MVP builds on, and they are
real in the code, not aspirational:

| Seam | Where | MVP consumer |
|---|---|---|
| `SandboxBackend` protocol (`provision`/`destroy`) | `packages/sandbox/src/sandbox/contract.py` | Hardened backends (§4.6) |
| Runner steps named as future activities (`create_run` … `teardown`) | `packages/runner/src/runner/flow.py` | Temporal activities (§4.3) |
| OPA input shape `{agent_id, role, run_id, tool, args}` | `packages/gateway/src/gateway/service.py`, `policies/poc.rego` | Compiled per-role bundles (§5.1) |
| Audit event schema (JSONL, args digested) | `packages/common/src/common/audit.py` | Audit store ingestion contract (§5.2) |
| Bearer-token → identity map (one function) | `packages/gateway/src/gateway/identity.py` | Workload identity / JWT (§4.9) |
| `telemetry.py` wrapping all logger creation | `packages/common` | OTel adoption |

What the PoC deliberately does **not** have: any notion of an organization
(one hardcoded identity), durability (a script that loses the run if it dies),
human gates, budgets, real identity, hardened sandboxing, or any workflow past
"PR opened."

---

## 2. What the MVP is

**One sentence:** a design partner writes a Company Blueprint, applies it to a
Kubernetes cluster, and gets a working, governed three-role software company —
PM, Backend Engineer, SDET — that takes a goal through **intake → implement →
review**, waits for a human on merge, survives crashes, stays inside budget,
and leaves a single audit story for everything it did.

The PoC proved invariants 1 and 2. The MVP keeps re-proving them on the new
runtime and adds three more. The five **platform invariants at MVP**:

1. **Agents hold no credentials.** Unchanged. No component in the agent's or
   sandbox's trust domain ever holds a git token or provider key — including
   new arrivals (orchestrator workers, brain frameworks, sandbox runtimes).
2. **No unmediated egress.** Unchanged in meaning; the enforcement primitive
   changes from Docker internal networks to Kubernetes NetworkPolicy plus an
   audited egress proxy for sandboxes.
3. **The company is declarative.** The Company Blueprint is the single source
   of truth; hire/fire/reorg are blueprint diffs; an operator converges live
   state; every enforcement artifact (Rego, infra policy, budgets, gate
   config) is a compiled projection of the blueprint.
4. **Company processes are durable and human-gated.** A feature can take days,
   wait on a human, and survive worker death. Process state lives in the
   orchestrator — never in an agent's memory. Cleanup (sandboxes, ephemeral
   identities) is guaranteed by compensation, not by `finally`.
5. **Spend is metered and bounded.** Every LLM token is attributed to
   `{company, run, standing parent, ephemeral participant, role}`; per-run caps
   hard-stop and escalate instead of silently degrading.

**Defense in depth completes at MVP.** The PoC shipped two of the three
layers; the MVP adds the third:

| Layer | Question | Mechanism at MVP |
|---|---|---|
| Infrastructure policy | What can the process physically touch? | NetworkPolicy per company namespace; sandbox backend (FS/process/resource/egress caps); egress proxy |
| Semantic policy | What does this action mean? | MCP gateway + OPA, per-role bundles compiled from the blueprint |
| Process policy | Who approved / may this continue? | Orchestrator workflows: HITL gates, timers, escalation, compensation |

A bypass of one layer must still be caught by another; no layer may be the
sole owner of a guarantee.

### What the MVP explicitly does not include

Deferred with intent (see [§13](#13-deferred-to-v1v2) for the full list):
release and incident workflows, Studio UI (API/CLI only), AGNTCY Identity
badges, Directory/OASF, SLIM as a hard dependency, gVisor/Kata as a
requirement, multi-tenancy hardening, tamper-proof audit storage, earned
autonomy, marketplace.

---

## 3. Architecture: five planes

The drafts each drew a fragment of this picture; this is the assembled whole.
The planes and their one-line ownership rules are the backbone of the design —
almost every collision the drafts identified is two planes trying to own the
same thing.

```text
┌───────────────────────────────────────────────────────────────────┐
│ INTENT — Company Blueprint (YAML + JSON Schema, git-versioned)    │
│ roles, crews, stack, tools, workflows+gates, policies, budgets    │
└───────────────────────────────┬───────────────────────────────────┘
                                │ compile (pure, diffable, previewable)
┌───────────────────────────────▼───────────────────────────────────┐
│ PLAN — compiled artifacts                                         │
│ Company/AgentInstance CRs · per-role Rego bundles · infra policy  │
│ IR (→ NetworkPolicy / Job spec / proxy allowlist / [eval] YAML)   │
│ crew templates · budget registrations · workflow gate config      │
└───────────────┬───────────────────────────────┬───────────────────┘
                │ reconcile                     │ configure
┌───────────────▼────────────────┐  ┌───────────▼───────────────────┐
│ REALITY — Company Operator     │  │ PROCESS — Durable Orchestrator│
│ + Registry (Postgres)          │  │ (Temporal, self-hosted)       │
│ namespace-per-company,         │  │ intake / implement / review   │
│ standing AgentInstances,       │  │ HITL gates, timers, retries,  │
│ NetworkPolicy, identity        │  │ compensation, run identity,   │
│ (standing roster only)         │  │ ephemeral crew fulfillment,   │
│                                │  │ sandbox lifecycle             │
└───────────────┬────────────────┘  └───────────┬───────────────────┘
                │ hosts                         │ dispatch / provision
┌───────────────▼────────────────────────────────▼──────────────────┐
│ EXECUTION — agent runtimes (standing + ephemeral brains)          │
│ + sandboxes behind SandboxBackend (credential-free, capped)       │
└───────────────────────────────┬───────────────────────────────────┘
                                │ every side effect, every model call
┌───────────────────────────────▼───────────────────────────────────┐
│ LAW — MCP gateway + OPA · LLM gateway (budgets) · audit store     │
│ authenticate → decide → audit → execute with platform credentials │
└───────────────────────────────────────────────────────────────────┘
```

**Ownership matrix** (the anti-collision table — each row is a rule that some
draft found violated or at risk):

| Plane / component | Owns | Must not own |
|---|---|---|
| Blueprint + compiler | Desired company shape; all enforcement artifacts as pure compile output; preview diffs | Cluster side effects; live credentials; runtime loops |
| Operator + registry | Convergence of **standing** structure; instance status; namespace + NetworkPolicy | Being a policy source of truth; runs; ephemeral agents |
| Orchestrator | Durability; HITL; compensation; run identity; **ephemeral crew fulfillment**; sandbox provision/destroy | Org truth; semantic branch/path rules; kernel enforcement; standing `AgentInstance` creation |
| Agent brain (any framework) | Local reasoning for one task | Credentials; sandbox lifecycle; cross-agent process state; spawning ungoverned sub-agents |
| SandboxBackend impl | Isolated workspace creation; applying compiled infra policy; teardown | Semantic rules; company lifecycle; reading blueprints |
| MCP gateway + OPA | AuthN/Z; semantic allow/deny; audit of every tool call; delivery with platform tokens | Being the HITL inbox; crash-resume; minting runtimes |
| LLM gateway | Provider keys; model-tier routing; metering; per-run hard-stops | Org truth; silently degrading instead of escalating |
| Audit store | One correlated story per run across all sources | Storing raw args/secrets (digests only) |

**Two control loops, never three.** The operator reconciles *what the company
is*; the orchestrator runs *processes against it*. Every third-party system
that wants to be a control plane (a workflow engine's own UI, OpenShell's
Gateway, a brain framework's session manager) is subordinated behind a
Poseidon contract. **[decided]**

---

## 4. The building blocks

The drafts disagreed about altitude — sandbox hardening asked whether it was a
peer pillar; the orchestrator doc demoted Temporal from pillar to preferred
engine. The consolidated hierarchy **[adopted]**:

- **Three pillars** (product-level building blocks):
  **A.** Company Blueprint (+ compiler, operator, registry) — invariant 3.
  **B.** Durable orchestrator — invariant 4.
  **C.** Flexible coding agents under governance — the work quality bet.
- **One blueprint extension:** standing & ephemeral agents (lifecycle
  semantics + spawn policy).
- **One subordinate plane:** sandbox hardening behind `SandboxBackend`
  (deliberately lower altitude: it implements isolation, it is not a product
  pillar).
- **Three shared services** (law-plane evolutions, each currently missing a
  draft doc or scattered across several): MCP gateway & compiled policy, LLM
  gateway & budgets, identity & audit.

### 4.1 Pillar A — Company Blueprint, compiler, operator, registry

**Purpose.** Prove invariant 3: a human describes the organization once, the
platform materializes and converges it. Hire, fire, and reorg are blueprint
diffs, not ops scripts.

**The blueprint** is a versioned YAML document validated by JSON Schema — not
a prompt pack, not a Helm chart, not the operator. Minimum MVP surface
**[decided]**:

| Section | Purpose |
|---|---|
| `company` | name, mission, product, stage |
| `stack` | languages, frameworks, repos, toolchain hints → sandbox image + egress allowlist |
| `departments.roles` | standing roles from a versioned role-template catalog: template, count, model tier, `may_spawn` |
| `crews` | ephemeral crew templates: members, per-child tool scopes, budget inheritance (§4.4) |
| `tools` | per-role MCP backend + method-level scopes → gateway allow rules |
| `workflows` | which SDLC processes are enabled; gate config (MVP: `review.gates.merge = human_approval`); drain defaults |
| `policies` | branch protection, path ownership, deny-paths, escalation |
| `budgets` | per-run caps (hard-stop), per-role/month tracking |

MVP roster: **PM, Backend Engineer, SDET** — three standing roles.
**[adopted]** The "(+ supervisor)" that appeared in one draft is dropped: no
document defines its behavior, and its function (watching runs, escalating,
gating) is exactly what the orchestrator plus HITL gates provide. If a
supervisor *agent* earns a job description later, it enters as a normal role
template.

**The compiler** is a pure function:
`(blueprint, role_catalog, registry_snapshot) → deployment_plan`. **[decided]**

It validates (schema + cross-checks: every workflow node maps to a hired
standing role; every tool scope names a registered gateway backend; every
`may_spawn.crew` references a known crew template; every child scope ⊆ its
standing parent's scope — violations fail at **compile**, not at first tool
call; budgets cover enabled workflows). It resolves role templates into
concrete agent specs. It emits **diffable** plans and supports **preview
before apply** (plan + projected cost via CLI/API). It emits, from one source
of truth, every enforcement projection:

- `Company` / `AgentInstance` CRs (standing roster only),
- per-role and per-crew-child Rego bundles targeting the decision input shape
  of §5.1,
- the **infrastructure policy IR** (§5.4) and its projections,
- crew-template artifacts, budget registrations, workflow gate config.

It must never: mutate cluster state, embed secrets into agent specs, or treat
OASF/Directory as a compile dependency.

**The operator** (Kubernetes operator, e.g. `kopf`-based) reconciles the CRs:
namespace-per-company, deny-all NetworkPolicy with egress only to the
gateways, standing agent Deployments, policy-bundle installation at the
gateways, teardown/revocation on delete. It owns **standing structure only**;
it never creates ephemeral agents and never runs processes. **[decided]**

**The registry** (Postgres) is the join point between declarative intent and
live state **[adopted** — the drafts referenced "the registry" without ever
specifying it; this makes it concrete**]**:

- `companies`, `agent_instances` (standing roster: identity, status),
- `run_participants` (ephemeral, keyed by `run_id` + parentage — separate
  table so hire/fire diffs stay clean, §4.4),
- `runs` (run ↔ workflow-id mapping, artifacts: ticket URLs, PR URLs, terminal
  status — replaces the PoC's `runs/<id>/manifest.json` files),
- `budget_ledger` (allocations, consumption, headroom — written by the LLM
  gateway),
- `sandbox_registrations` (run → sandbox address; today an in-memory dict in
  the gateway, which cannot survive gateway restart once runs are durable).

The compiler diffs against a registry snapshot; the operator writes instance
status; the orchestrator writes runs and participants; nothing else writes.

**Runtime consequence — Kubernetes only. [decided]** This block is where
Poseidon leaves Docker Compose. Operator, namespace-per-company,
NetworkPolicy, CR-driven instances. Local development uses a small cluster
(`kind`/`k3d`/`minikube`) on the **same** Helm/operator path as production.
Compose is not maintained in parallel — consistent with the no-backwards-
compatibility stance, it is deleted as soon as the E1–E4 equivalents pass on
Kubernetes (§10, M1), not kept as a frozen bridge. **[adopted** — the drafts
wavered between "frozen regression bridge, then delete" and "delete";
resolved to the earliest honest deletion point**]**

**Exit criteria (this pillar):** from `hello-saas.yaml`: compile → preview →
apply → operator converges a live company; a blueprint edit (hire a role,
change a tool scope) converges without manual steps; teardown revokes
everything, verified by a failed post-teardown gateway call.

### 4.2 Pillar B — Durable orchestrator

**Purpose.** Prove invariant 4. Turn the PoC's script runner into the process
plane: crash-resumable SDLC workflows, HITL gates, guaranteed compensation.

**Engine: Temporal, self-hosted, behind thin seams. [decided]** Temporal is
the preferred engine, not a platform pillar: workflows and activities keep the
step-shaped functions of the PoC runner behind a thin adapter so the engine
stays swappable in principle (O1 = B). Self-hosted on the platform's own
Kubernetes path, pinned in Helm, runnable on kind — no Temporal Cloud
dependency (O3 = B). Workers run in the platform namespace; agent reasoning
stays in company namespaces (O7 = C, hybrid).

**Package: `packages/runner` → `packages/orchestrator`. [decided]** The CLI
becomes a thin client (`poseidon run|signal|status`) that starts, signals, and
queries workflows. Suggested shape:

```text
packages/orchestrator/
  cli/           # start / signal / status (thin)
  workflows/     # intake, implement, review
  activities/    # thin clients over gateway / SandboxBackend / agent HTTP
  gates/         # signal handlers, timers, escalation
  worker/        # hosts workflows + activities
```

**Workflow set: intake → implement → review. [decided]** (O2 = B; release and
incident are V1.) Detailed in [§6](#6-the-mvp-workflows-in-detail).

**Activities stay thin.** They call the existing planes — gateway client,
`SandboxBackend`, agent HTTP — and never become a second chokepoint or a
second secret bag: no `GIT_TOKEN`, no provider keys in workers, ever. The
orchestrator token (successor of the PoC runner token) is a privileged
identity and is treated as such in policy.

**HITL is an orchestrator primitive.** Workflows park on signals configured by
blueprint `workflows.*.gates`; approval arrives via API signal in MVP (Studio
inbox is V1); timers escalate per blueprint config when nobody responds.

**The orchestrator owns all run-scoped lifecycle:** sandbox provision/destroy
(calling `SandboxBackend`), and ephemeral crew fulfillment (§4.4). Guaranteed
compensation on success, failure, cancel, timeout, and worker death — the
consolidated failure matrix is in [§6.4](#64-consolidated-failure-matrix).

**Run identity. [decided]** `run_id` remains the canonical correlation key
(minted by `create_run`, as in the PoC). The Temporal workflow id is derived
deterministically from it (e.g. `implement-<run_id>`), and the registry `runs`
table stores the mapping plus external artifacts (tickets, PRs) so the E4
promise — the whole story reconstructable — survives at MVP scale.

**Exit criteria (this pillar):** kill the worker mid-`implement`, restart:
the workflow resumes or compensates, no leaked sandbox or identities, run
record terminal and honest; run intake → implement → review, park on the merge
gate, approve via API signal, observe the gate outcome in the same
`run_id`-correlated audit story as the gateway decisions.

### 4.3 Pillar C — Flexible coding agents under governance

**Purpose.** Arbitrary software tasks, not just "small change, tests pass,
open PR." The reasoning loop gets much more capable; governance does not
relax one bit.

**The three-layer split [decided]:**

| Layer | Owns | Must not own |
|---|---|---|
| **Brain** — coding-agent runtime (OpenHands-class SDK or peer) | Reasoning loop, context engineering (condensers, skills, memory), coding heuristics and recovery | Credentials, direct git/network egress, policy truth, sandbox lifecycle, sub-agent spawning outside governance |
| **Hands** — adapters mapping the brain's tools to gateway MCP calls | Hiding plumbing (e.g. `run_id` injection, bundle composition — both already exist in the PoC toolbox) | Any bypass around the gateway |
| **Law** — gateway + OPA + audit + delivery | Everything it owns today, plus the verify gate | Being replaced by a framework's own "security analyzer" (defense-in-depth at most) |

**Framework adoption rules [decided]:** adopt the framework *as a library
behind Poseidon contracts*. Its tools resolve to gateway MCP only. It does not
start or stop sandboxes (the orchestrator does). It does not own retries,
timeouts, or cancellation at the process level (the workflow does — the
activity dispatches *into* the brain and waits). Its internal sub-agents, if
any, are not Poseidon agents and must not acquire gateway identity outside the
crew mechanism (§4.4). Delivery remains bundle → gateway policy → platform
push.

**Verification moves out of the brain. [adopted]** The PoC graph owns
verify-before-deliver; the flexible-agents draft demands it become platform
policy but does not say where it lands. Resolution: the **implement workflow**
owns the verify gate — a workflow activity runs the blueprint-configured test
command via the gateway (`sandbox.run_tests`) after the brain claims
completion and before delivery is accepted; red results are fed back for a
bounded number of iterations (policy allows the orchestrator role to invoke
run_tests for the run). Whether the gate must *also* be visible to OPA is part
of [O-1](#12-open-decisions).

**Concrete framework choice: [open]** (O-2). OpenHands Agent SDK is the
leading candidate; the choice is made after a short adoption spike, not by
default. The PoC LangGraph agent is kept as the regression brain until the
new brain passes the same exit tests, then deleted (no dual maintenance).

**Sequencing guard [decided]:** do not adopt the new brain and a new sandbox
runtime (OpenShell) in the same slice — stacked integration risk. Hands→Law
plus orchestrator-owned lifecycle land first, on the boring sandbox backend.

**Exit criteria (this pillar):** the new brain, driven through Hands→Law,
passes the same happy-path and injection tests as the PoC brain on a
non-trivial task; a framework-initiated action outside its tool scope is
denied at the gateway and audited, not merely discouraged.

### 4.4 Blueprint extension — standing and ephemeral agents

**Purpose.** The lifecycle axis the blueprint alone does not answer: which
constructs belong to the *company* (survive reorgs, stable identity) and which
exist only for a *task* (spawned, collaborate, disappear).

**Vocabulary [decided]** (O1 = A): `standing` / `ephemeral` are the schema and
platform enums; "crew" names a *template* for a set of ephemeral members, not
a lifecycle class. This axis is **not** the listener/worker compute-packing
hybrid from the historical specs — a standing agent may scale to zero and an
ephemeral agent still needs real identity; do not collapse the two axes.

**The rules [decided]:**

1. **Standing** agents are blueprint-declared, operator-reconciled registry
   rows with stable `agent_id`. Created/changed/removed only by blueprint
   apply.
2. **Ephemeral** agents exist only inside a run: created with
   `{run_id, parent_agent_id, crew_template, reason}`, recorded in
   `run_participants` (separate store, O4 = B), torn down on every terminal
   path with the same seriousness as sandboxes. No promotion to standing
   except via a blueprint diff.
3. **Spawn is request-then-fulfill** (O2 = C): the standing agent calls a
   governed `crew.request` tool; gateway + OPA check `may_spawn`, concurrency
   caps, and budget headroom; the **orchestrator** fulfills — mints short-lived
   identities, installs run-scoped policy projections, dispatches members,
   revokes and tears down at the end. An allowed request is authorization +
   intent, never fulfillment; requests are idempotent and expire.
4. **Child scopes are explicit and ⊆ the standing parent's scope** (O3 = B),
   enforced at compile time.
5. **Every call carries parentage:** ephemeral callers hit the gateway with
   `{company, parent_agent_id, ephemeral agent_id, role, run_id}` so policy,
   audit, and budgets tell one story (§5.1, §5.2).
6. **Crew coordination is an orchestrator-fixed graph in MVP** (planner →
   one executor); free-form crew chat is deferred.
7. Ephemeral agents never hold SLIM subscriptions in MVP and are never
   Directory/OASF citizens.

**First concrete crew [decided]:** `feature-delivery` — a standing PM
requests a planner plus one coding executor (executor reuses the
backend-engineer template and the Pillar-C brain) for an `implement` run.

**Who executes `implement`? [adopted]** The drafts pull two ways: the
blueprint maps workflow nodes to standing roles (implement → standing
engineer), while this extension's first proof has the PM's ephemeral crew
delivering implement. Resolution: **both paths exist and both are exercised.**
The core implement exit criterion runs against the standing Backend Engineer
(simplest, no crew machinery on the critical path). The crew path is the
*proof of this extension*, run as its own exit criterion. The known tension
that a PM's standing scope must then cover `sandbox.*` for the ⊆ rule to hold
is resolved per the draft's own mitigation — widen the PM's *declared* allows
(enforcement still restricts what the PM's own brain may call via
phase/policy); if that proves ugly in practice, the fallback is nesting coding
crews under the standing engineer instead. Review this when the first crew is
wired.

**Exit criteria (this extension):** PM requests `feature-delivery`; planner
and executor appear as `run_participants` with correct parentage on every
gateway decision; both are gone (identities revoked, rows terminal) after
teardown — including when the worker is killed mid-crew; an unauthorized
`crew.request` is denied by rule; a child scope ⊄ parent fails at compile.

### 4.5 Subordinate plane — sandbox hardening behind `SandboxBackend`

**Purpose.** Turn "no credentials + no route" into a real isolation plane —
disposable workspaces with enforced filesystem, process, resource, and network
boundaries — without making any vendor runtime the foundation. Deliberately
lower altitude than the pillars: it *implements* isolation. **[decided]**

**Contract [decided]** (O5 = A + C): still two lifecycle methods, provision
gains a compiled policy argument:

```python
class SandboxBackend(Protocol):
    def provision(self, run_id: str, workspace_tar: bytes,
                  policy: SandboxPolicy) -> SandboxHandle: ...
    def destroy(self, handle: SandboxHandle) -> None: ...
```

`SandboxPolicy` is the compiled infra-policy IR of §5.4: image/toolchain,
egress allowlist, filesystem scope, CPU/memory/disk caps, wall-clock deadline.
Only the orchestrator calls provision/destroy. **[decided]**

**MVP default backend: Kubernetes Jobs + NetworkPolicy + resource caps.
[adopted]** The draft left the default open between "hosted (E2B/Modal-class)"
and "K8s Jobs". Resolution: K8s Jobs, because the two decisions already taken
elsewhere — Kubernetes-only deployment and self-hosted-only Temporal — express
a clear platform stance of *no external runtime dependencies on the critical
path*; adding a hosted sandbox vendor would contradict it, and plain Jobs with
deny-all NetworkPolicy, resource limits, and `activeDeadlineSeconds` is boring
and sufficient for one design-partner company. A hardened `RuntimeClass`
(gVisor/Kata) is optional at MVP, required no earlier than V1. Hosted remains
a documented contingency, not a built backend.

**OpenShell: evaluation track, strictly non-gating. [decided]** Pin a
version, run the red-team suite against it, keep it behind the contract with
**zero credential providers** and inference routing either disabled or pointed
at the LLM gateway. The V1 decision gate stands: if it matures past alpha
with a workable multi-tenant posture it can become the default and the
self-operated hardening path is never built; otherwise `k8s-jobs` proceeds.
Its Gateway never reads blueprints or the registry; its OCSF audit export is
an ingest adapter into our audit schema, never a parallel silo.

**Egress: deny-by-default with an audited proxy. [adopted]** The PoC's
zero-egress sandbox is too rigid once arbitrary stacks need `pip`/`npm`, and
the flexible brain (§4.3) makes package installs a near-certainty. MVP ships
the **registry/egress proxy**: sandbox NetworkPolicy allows egress *only* to
the proxy; the proxy enforces the blueprint-compiled allowlist and **emits an
audit event for every deny** (this is what makes the "egress attempt blocked
*and* visible in the audit store" exit criterion mechanically satisfiable —
NetworkPolicy itself drops packets silently and serves as the backstop
beneath the proxy). Blueprints whose stack needs nothing get zero-egress as
before.

**Backends layout:**

```text
packages/sandbox/
  contract.py      # provision(run_id, workspace_tar, policy) / destroy
  toolserver/      # unchanged role: file ops, exec, tests, bundle — gateway-only ingress
  backends/
    k8s_jobs/      # MVP default
    openshell/     # evaluation track, non-gating
    docker/        # deleted with Compose (M1)
```

**Exit criteria (this plane):** a sandboxed agent attempting egress to a
non-allowlisted host is blocked by the live infra layer and the attempt
appears in the audit store correlated by `run_id`; sandbox resource caps and
deadlines demonstrably terminate a runaway workload; the red-team suite
(egress exfiltration, secret scrape, breakout attempts) runs against the live
backend, not only against OPA denies.

### 4.6 Shared service — MCP gateway and compiled semantic policy

The gateway keeps its PoC pipeline (authenticate → decide → audit → execute)
and its design stance (even the agent's own sandbox calls go through it).
What changes at MVP:

- **Policy becomes compiled.** `policies/poc.rego` and the static
  `token → identity` map are retired the moment the first compiled company
  passes the governance regression suite (M2); per-role and per-crew-child
  Rego bundles compiled from the blueprint are installed by the operator,
  targeting the extended decision input (§5.1). No second policy engine, no
  "declarative mode" special case. **[decided]**
- **The tool surface grows** to cover the three workflows **[adopted** — the
  drafts implied this without ever listing it**]**:

| Tool | New/existing | Consumer |
|---|---|---|
| `repo.archive`, `sandbox.*`, `git.open_pr` | existing | implement |
| `issues.create / read / update / comment` | **new** backend | intake (PM), review findings |
| `git.read` (PR metadata, diffs) | **new** | review (SDET) |
| `git.comment` (review findings on the PR) | **new** | review (SDET) |
| `git.merge_pr` | **new** | orchestrator, only after the merge gate (see O-1) |
| `crew.request` | **new** | standing agents with `may_spawn` |
| run/sandbox registration | existing admin surface | orchestrator (registry-backed, restart-safe) |

- **Issue tracker choice: GitHub Issues. [adopted]** Intake needs a ticket
  backend and no draft names one. GitHub Issues keeps MVP at one external
  host and one platform credential.
- Path-ownership rules from the blueprint become enforceable because the
  bundle-apply trick already gives policy the changed paths. **[decided]**

### 4.7 Shared service — LLM gateway and budgets

No draft document exists for this block; every draft depends on it. It is a
first-class MVP building block and needs its own rewrite doc. **[adopted]**

- **Base:** the stock LiteLLM proxy grows into the **LLM gateway** — same
  credential-isolation role (provider keys live only here), plus:
- **Caller identity:** agents authenticate to it with the same workload
  identity as at the MCP gateway, and every call carries run context. (Today
  the proxy is anonymous passthrough — attribution is impossible without
  this.)
- **Metering & attribution:** every request is metered and written to the
  registry budget ledger under `{company, run_id, standing parent, ephemeral
  participant, role}` — one ledger, dimensions rather than parallel accounts,
  so crew fan-out cannot create unreconcilable spend.
- **Model-tier routing:** blueprint `model_tier` per role resolves here.
- **Hard-stops:** exhausting a per-run cap stops further model calls **and
  signals the orchestrator**, which pauses the workflow and escalates
  (API-visible pending state). Never silent degradation. Budget events land
  in the audit store.

**Exit criteria:** a run that exhausts its blueprint budget is paused with an
escalation and its spend is fully attributed in the ledger; no model call is
possible without an authenticated identity and a run context.

### 4.8 Shared service — identity

- **MVP: internal workload identity.** Short-lived JWTs (Kubernetes service
  account tokens or equivalent) verified at both gateways replace the two
  static bearer tokens. The PoC's one-lookup identity function is exactly the
  seam being replaced. **[decided]**
- **Ephemeral identities** are minted by orchestrator fulfill activities and
  revoked on teardown — revocation is mandatory, tested by the kill-worker
  exit criterion. **[decided]**
- **AGNTCY Identity badges stay off the MVP critical path** (V1, org
  boundary). **[decided]**

### 4.9 Shared service — audit store and the one run story

The PoC's E4 ("the whole story from the log alone") must survive four new
event sources. **[adopted** — drafts agreed on the goal (sandbox draft O8
preferred "infra denies visible in the same store") but never named the
store; resolved as follows**]**

- **One ingestion contract**, extending the PoC `AuditEvent` schema (§5.2).
- **Sources:** MCP gateway decisions (as today), process-gate outcomes
  (approve/reject/timeout, from the orchestrator), sandbox infra denies (from
  the egress proxy; OpenShell OCSF via adapter on the eval track), and budget
  events (from the LLM gateway).
- **Sink at MVP:** a table in the registry Postgres — queryable, honest, no
  new infrastructure. The JSONL file retires with Compose. Tamper-proofing is
  explicitly deferred.
- **Correlation:** every event carries `run_id`; ephemeral events carry
  parentage; gate outcomes carry the workflow id that maps back to `run_id`.

---

## 5. Cross-cutting contracts

These are the frozen interfaces between building blocks — the MVP equivalents
of the PoC's C1–C8. Getting these right matters more than any single block's
internals.

### 5.1 Policy decision input (extends the PoC shape)

```json
{
  "company": "hello-saas",
  "agent_id": "backend-engineer-1",
  "lifecycle": "standing | ephemeral | platform",
  "parent_agent_id": null,
  "role": "backend-engineer",
  "run_id": "run-…",
  "workflow": "implement",
  "tool": "git.open_pr",
  "args": { "branch": "feature/x", "paths": ["src/…"] }
}
```

Strict superset of the PoC's `{agent_id, role, run_id, tool, args}`; compiled
bundles target this shape; parentage fields are mandatory for ephemeral
callers (audit reconstruction breaks without them). The `platform` lifecycle
covers the orchestrator's own privileged calls.

### 5.2 Audit event (extends the PoC schema)

PoC fields (`ts, run_id, agent_id, role, tool, args_digest, decision, rule,
latency_ms, backend_status`) plus: `company`, `lifecycle`, `parent_agent_id`,
`workflow`, and `source` (`gateway | llm-gateway | sandbox-infra |
process-gate`). Arguments remain digests — audit must never become a leak
channel.

### 5.3 Sandbox contract v2

As §4.5: `provision(run_id, workspace_tar, policy) -> SandboxHandle`,
`destroy(handle)`. The handle gains what the backend needs (pod/job reference
instead of container id) without widening the method set.

### 5.4 Infrastructure policy IR

One neutral IR compiled from blueprint `stack` + role + policies **[decided]**
(sandbox draft O3 = B), projecting to: namespace NetworkPolicy (outer
envelope, operator-owned), Job spec constraints (resource caps, deadline,
optional RuntimeClass), egress-proxy allowlist config, and — evaluation track
only — OpenShell policy YAML. One truth, several emissions; hand-authoring
any projection is a bug.

### 5.5 Run identity

`run_id` is canonical everywhere (audit, ledger, participants, sandbox
registration). Workflow ids derive from it; external artifacts (tickets, PRs)
are recorded against it in the registry `runs` table.

---

## 6. The MVP workflows in detail

### 6.1 intake

Goal text in (CLI/API) → `create_run` + budget reservation → PM agent
dispatched to scope → PM writes tickets through `issues.*` gateway tools →
run record links the tickets. No human gate by default in MVP (configurable
via blueprint like any other gate).

### 6.2 implement

The PoC's five steps become activities, reshaped by the platform-owned verify
gate:

| Activity | Body (thin client over existing planes) |
|---|---|
| `create_run` | Mint/correlate `run_id`; registry run row; budget check |
| `fetch_workspace` | Gateway `repo.archive` |
| `provision_sandbox` | `SandboxBackend.provision(run_id, tar, compiled policy)`; registry-backed sandbox/run registration |
| `dispatch_work` | Agent HTTP dispatch (standing engineer, or crew executor via §4.4); wait for the brain's completion claim |
| `verify` | Platform-run `sandbox.run_tests` via gateway; red → feed back and re-dispatch, bounded by blueprint config |
| `request_delivery` | Agent delivers: commit on `feature/*` → bundle → `git.open_pr` through the gateway (mechanics unchanged from PoC) |
| `record_artifacts` | PR URL etc. into the registry |
| `teardown` | Destroy sandbox, revoke ephemeral identities, finalize run — on every terminal path, including compensation |

### 6.3 review

PR → review sandbox provisioned from the PR ref (the SDET needs to *run*
things, not just read diffs — a detail no draft covered; flagged in O-4) →
SDET reviews (reads diff via `git.read`, executes tests in its sandbox, posts
findings via `git.comment`/`issues.comment`) → **merge gate**: workflow parks
on the blueprint-configured signal → human approves/rejects via API →
approve: orchestrator merges via `git.merge_pr`; reject: findings loop or
close per blueprint config → gate outcome written to the audit store.

**Transport / SLIM. [adopted]** The drafts contradict each other here
("review is the first load-bearing SLIM use" vs. "do not block implement or
review on SLIM maturity"). Resolution: review at MVP is
**orchestrator-sequenced over HTTP** — activities dispatch the reviewer and
collect findings; there is no free-form group session on the critical path.
Group messaging stays behind the App SDK transport seam; a SLIM group session
for review is a non-gating stretch inside the MVP window and becomes
load-bearing at V1 (with incident war-rooms). Nothing in the MVP exit
criteria mentions SLIM.

### 6.4 Consolidated failure matrix

The load-bearing rows only (both drafts' matrices, merged and deduplicated) —
everything else is documented, not built:

| Failure | MVP behavior |
|---|---|
| Worker/process dies mid-activity | Resume from history; teardown compensation guarantees no leaked sandbox, identity, or participant row |
| Sandbox dies mid-implement | Fail the run cleanly + teardown; no silent re-provision loops |
| Agent/brain timeout or never finishes | Workflow owns the timeout; mark failed; teardown; honest terminal record |
| Human never approves the merge gate | Timer → escalate per blueprint gate config; API-visible pending state |
| Budget hard-stop mid-run | LLM gateway signals; workflow pauses + escalates; no further spawns |
| `crew.request` allowed but fulfillment never runs | Requests idempotent and expiring; no orphan identities |
| Standing parent drained mid-crew | Blueprint drain default (MVP default: abort crew) |

Deferred to V1+: review deadlocks, snapshot-and-park, close-abandoned-PR
polish, partial `open_pr` crash recovery beyond basic idempotency, drain edge
cases beyond blueprint defaults.

---

## 7. Deployment model

- **Kubernetes only**, one architecture for local (`kind`/`k3d`/`minikube`)
  and production, via the same Helm + operator path. **[decided]**
- Platform namespace: operator, Temporal + workers, MCP gateway, LLM gateway,
  OPA, registry (Postgres), egress proxy. Company namespaces: standing agent
  runtimes, ephemeral crew pods, sandbox Jobs — deny-all NetworkPolicy, egress
  only to the gateways (and the proxy for sandboxes).
- Postgres and Temporal self-hosted in-cluster for MVP (managed Postgres is an
  acceptable ops choice later; nothing may *depend* on managed services).
  **[adopted]**
- `deploy/compose/` and the Docker sandbox backend are **deleted at M1**
  (§10) once the governance regression suite passes on Kubernetes. No dual
  stack, per the no-backwards-compatibility stance. **[adopted]**

---

## 8. Repository restructuring

| Package | Fate at MVP |
|---|---|
| `packages/schema` | **new** — blueprint JSON Schema, role-template catalog, examples (`hello-saas.yaml`) |
| `packages/compiler` | **new** — blueprint → CRs, Rego bundles, infra IR, crew templates, budgets, gate config; preview/diff |
| `packages/operator` | **new** — `Company`/`AgentInstance` CRDs + reconciler |
| `packages/registry` | **new** — Postgres models + API (instances, participants, runs, ledger, audit) |
| `packages/runner` → `packages/orchestrator` | **renamed & rebuilt** — workflows, activities, gates, worker, thin CLI |
| `packages/gateway` | **evolved** — JWT auth, compiled bundles, new backends (issues, git.read/comment/merge, crew.request), registry-backed registrations |
| `packages/gateway-llm` | **new** — LiteLLM-embedded LLM gateway: routing, metering, budgets, hard-stop signaling |
| `packages/agent` | **rebuilt** — brain host: flexible framework behind the Hands adapter; PoC graph kept only until parity, then deleted |
| `packages/sandbox` | **evolved** — contract v2, `k8s_jobs` backend, egress proxy, `openshell` eval backend; `docker` backend deleted at M1 |
| `packages/common` | **evolved** — audit schema v2, ids, telemetry (OTel enters here) |
| `policies/` | Rego *templates* consumed by the compiler + tests; `poc.rego` deleted at M2 |
| `crds/`, `deploy/helm/` | **new**; `deploy/compose/` deleted at M1 |

---

## 9. Sequencing

Four milestones; tracks within a milestone can run in parallel. This
consolidates the three drafts' separate sequencing proposals (durable O8 =
D + A, sandbox O6 = A/C-parallel, standing O5 = B) into one line:

**M1 — the PoC, re-hosted and durable.** Kubernetes + Helm + kind; Temporal
self-hosted; `packages/orchestrator` with the durable `implement` workflow on
*hardcoded* gates; `k8s_jobs` sandbox backend + egress proxy; existing PoC
brain and policy. Exit: E1–E4 equivalents pass on Kubernetes **plus**
kill-the-worker crash-resume. Then delete Compose and the Docker backend.

**M2 — the declarative company.** Schema, compiler (with preview), CRDs,
operator, registry; compiled per-role bundles and workload-identity JWTs
replace `poc.rego` and static tokens; infra IR feeds the sandbox backend.
Exit: blueprint edit converges a live company; governance suite passes against
compiled policy. Then delete `poc.rego` and the token map.

**M3 — the multi-role SDLC.** intake + review workflows; issues/git gateway
backends; HITL merge gate wired to blueprint config; LLM gateway metering +
hard-stops; audit store consolidation; standing/ephemeral: `crew.request` →
fulfill → `run_participants`, first crew on implement.

**M4 — the flexible brain (overlaps M3).** Framework spike → adoption behind
Hands; platform verify gate; red-team suite against the live sandbox backend;
OpenShell evaluation track (non-gating) alongside.

Hard sequencing rules: crash-resume work (M1) blocks on nothing external —
not OpenHands, not OpenShell, not SLIM; the full intake→review exit criterion
is claimed only with blueprint-driven gate config (not hardcoded gates); brain
and sandbox-runtime adoption never land in the same slice.

---

## 10. Consolidated MVP exit criteria

The MVP is done when all of the following pass, scripted, on a cluster
deployed from the Helm/operator path:

1. **Declarative:** from `hello-saas.yaml` — compile, preview, apply; the
   operator converges the company; a blueprint edit converges live without
   manual steps; teardown revokes everything (verified by a failed
   post-teardown gateway call).
2. **Durable:** kill the orchestrator worker mid-`implement`, restart — the
   workflow resumes or compensates; no leaked sandbox, identity, or
   participant row; run record terminal and honest.
3. **End-to-end:** intake → implement → review on a real repo: PM tickets on
   the issue tracker, PR opened via the gateway, SDET findings posted, merge
   gate parked, human approves via API signal, gateway merges — every step
   correlated by `run_id` in one audit story.
4. **Governance re-proof:** the four PoC criteria hold on the new runtime
   against *compiled* policy — happy path; injected "deliver on main" denied
   by rule, audited, recovered; egress attempts from agent and sandbox
   blocked, with the sandbox attempt visible in the audit store; the full
   story (tool calls, infra denies, gate outcomes, budget events)
   reconstructable from the audit store alone.
5. **Budgets:** exhausting a per-run cap hard-stops model calls, pauses the
   workflow, and escalates; the ledger attributes all spend to
   `{run, parent, participant, role}`.
6. **Standing/ephemeral:** the PM requests `feature-delivery`; planner and
   executor operate under `run_id` with parentage on every decision; both are
   fully revoked after teardown, including under worker death; unauthorized
   spawn is denied by rule; a child scope ⊄ parent fails at compile.
7. **Parity:** all of the above runs identically on a local kind cluster and
   a real cluster.

---

## 11. Resolved contradictions and cleanups

The rewrite of the individual building-block docs should reflect these
resolutions. Each row names where the drafts disagreed (or were silent) and
what this document says.

| # | Drafts said | Resolution here |
|---|---|---|
| 1 | Ambiguous altitude: sandbox doc debated peer-vs-subordinate; Temporal/OpenShell sometimes read as pillars | Fixed hierarchy: 3 pillars, 1 blueprint extension, 1 subordinate plane, 3 shared services (§4) |
| 2 | "Review is the first load-bearing SLIM use" vs. "don't block on SLIM" | Review is orchestrator-sequenced HTTP at MVP; SLIM behind the App SDK seam, non-gating stretch; load-bearing at V1 (§6.3) |
| 3 | Implement executed by standing engineer (blueprint) vs. PM's ephemeral crew (standing/ephemeral) | Both paths, both exercised: standing engineer is the core exit path; the crew is the extension's own proof; PM-scope tension resolved by widening declared allows, reparenting as fallback (§4.4) |
| 4 | "(+ supervisor)" listed in the blueprint roster, defined nowhere | Dropped from MVP; orchestrator + HITL gates supervise (§4.1) |
| 5 | Default sandbox backend left open: hosted vs. K8s Jobs | K8s Jobs + NetworkPolicy + caps; consistent with K8s-only and self-hosted-only stances; hosted is a documented contingency, not built (§4.5) |
| 6 | Zero-egress sandboxes vs. brains needing `pip`/`npm` | Egress proxy with blueprint-compiled allowlist ships at MVP; proxy denies are audited; NetworkPolicy is the silent backstop (§4.5) |
| 7 | Compiler emits OpenShell YAML with possibly no consumer (C6) | Neutral infra IR with projections; OpenShell projection exists only on the eval track (§5.4) |
| 8 | Verify gate: PoC brain-owned vs. "platform policy" with no location | The implement workflow owns verify as an activity; brain testing is advisory (§4.3, §6.2) |
| 9 | Audit "store" invoked everywhere, defined nowhere; JSONL still the only sink | One ingestion contract, four sources, Postgres sink in the registry at MVP; JSONL retires with Compose (§4.9) |
| 10 | LLM budgets referenced by every doc, no doc exists | Promoted to a first-class shared service with defined behavior; needs its own doc in the rewrite (§4.7) |
| 11 | Intake needs an issue tracker; none named | GitHub Issues via a new gateway `issues.*` backend (§4.6) |
| 12 | "Reviewer + SDET" in the review workflow, with a 1-engineer roster | SDET is the MVP reviewer; the human holds merge (§6.3) |
| 13 | Compose "frozen bridge, then delete" vs. K8s-only | Deleted at M1, the earliest point the governance suite passes on K8s; no frozen bridge (no-backwards-compat stance) (§7) |
| 14 | `poc.rego` + static tokens "kept as governance proof" | Kept only until M2's compiled-policy regression passes, then deleted (§9) |
| 15 | Run identity split-brain risk (C5) named, not resolved | `run_id` canonical; workflow ids derived; registry `runs` table maps artifacts (§5.5) |
| 16 | Gateway sandbox registry is in-memory; incompatible with durable runs | Registrations move to registry-backed storage; gateway restart-safe (§4.1, §6.2) |

---

## 12. Open decisions

Genuinely open — do not let implementation hard-wire these by accident.

- **O-1 — Do process approvals appear in OPA?** (durable draft O6/C4.) The
  fork: (A) workflow state alone gates sensitive calls (`git.merge_pr` is
  simply only called after the gate), vs. (B) the gate emits an approval
  artifact that OPA independently requires on sensitive tools.
  **Recommendation:** for MVP, A *plus* first-class gate-outcome events in the
  audit store, with `git.merge_pr` callable only by the orchestrator identity;
  commit now to revisiting B at V1 (release/deploy gates raise the stakes).
  The cost of A is that OPA cannot independently prove approval — acceptable
  at MVP's trust level, but this is a security-posture call that needs
  explicit sign-off.
- **O-2 — Which brain framework?** OpenHands Agent SDK is the named
  candidate; decide after a bounded spike. Criteria: tools cleanly resolvable
  to gateway MCP; no mandatory ownership of workspace/session lifecycle;
  context machinery (condensers/skills) usable headless; license and pace of
  change.
- **O-3 — Hands tool-surface width.** Does the adapter map the brain onto the
  existing six `sandbox.*` tools, or does the sandbox toolserver grow richer
  primitives (patch-apply, search) for fidelity? Decide during the O-2 spike;
  affects toolserver scope.
- **O-4 — Review sandbox shape.** Full sandbox from the PR ref (SDET can
  execute), or read-only diff review via `git.read` only? Recommendation:
  full sandbox (it reuses the implement plumbing verbatim), but it doubles
  sandbox count per feature — a cost call.
- **O-5 — OpenShell V1 gate criteria.** Define *before* the gate: multi-tenant
  posture compatible with namespace-per-company; API stability compatible
  with Helm pinning; K8s driver viability kind→prod; clean subordinate mode;
  red-team green; OCSF ingest proven; honest cost comparison vs. operating
  `k8s_jobs` hardening ourselves.

---

## 13. Deferred to V1/V2

Documented so their absence is a decision, not an oversight:

- **V1:** release + incident workflows; Studio UI (approval inbox, org chart,
  run timelines, budget dashboards, audit search); AGNTCY Identity badges at
  the org boundary; SLIM group sessions as load-bearing (review, incident
  war-rooms); hardened sandbox runtime decision gate (OpenShell vs. gVisor/
  Kata `k8s_jobs`); multi-company on shared infra with per-company data
  isolation; richer drain/reassign; two-backend sandbox equivalence proof;
  approval artifacts in OPA (per O-1's revisit commitment).
- **V2:** marketplace via Directory/OASF (role templates as export format —
  never a compile dependency); earned autonomy (gate config mutated via
  controlled overlays backed by eval data — never model self-authorization);
  cross-company federation; ephemeral-crew free-form collaboration; SOC2-
  oriented audit deliverables; blueprint authoring copilot.

---

## 14. Doc set for the rewrite

Suggested target structure for the clean building-block documents, replacing
the current five drafts:

1. `company-blueprint.md` — Pillar A intent+plan (blueprint, schema, compiler,
   preview) — §4.1 minus operator/registry.
2. `operator-and-registry.md` — Pillar A reality (CRDs, reconciliation,
   registry schema, K8s-only deployment) — §4.1 remainder + §7.
3. `durable-orchestrator.md` — Pillar B — §4.2 + §6.
4. `standing-and-ephemeral-agents.md` — blueprint extension — §4.4.
5. `flexible-coding-agents.md` — Pillar C — §4.3.
6. `sandbox-plane.md` — subordinate plane incl. egress proxy — §4.5.
7. `llm-gateway-and-budgets.md` — **new**, currently missing entirely — §4.7.
8. `gateway-identity-audit.md` — **new**: MCP gateway evolution, workload
   identity, audit store, cross-cutting contracts — §4.6, §4.8, §4.9, §5.

Each rewritten doc should carry: purpose, what changes from PoC, decisions
(with the [decided]/[adopted]/[open] provenance from this document), owned
contracts, exit criteria, and its row of the ownership matrix (§3).
