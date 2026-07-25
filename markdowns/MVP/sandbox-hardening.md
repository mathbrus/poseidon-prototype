# MVP direction: sandbox hardening behind `SandboxBackend`

> **Status:** work in progress. Direction notes for review — not a frozen
> implementation plan. Cross-walked against
> `[company-blueprint.md](company-blueprint.md)` and
> `[flexible-coding-agents.md](flexible-coding-agents.md)`; lineage from
> `[../poc/specification3.md](../poc/specification3.md)` and
> `[../poc/prototype.md](../poc/prototype.md)`.

## Goal

The PoC proves two invariants with a **plain Docker sandbox**: agents hold no
credentials, and the sandbox has zero egress. That is enough for E1–E4. It is
not enough once companies bring arbitrary stacks, long-horizon coding agents
run open-ended shell/edit loops, and generated code is treated as untrusted.

**Sandbox hardening** is the MVP building block that turns the existing narrow
`SandboxBackend` contract into a real isolation plane: disposable, credential-
free workspaces with enforced filesystem, process, resource, and network
boundaries — without making any one vendor runtime the platform foundation.

NVIDIA **OpenShell** is the *preferred candidate implementation* of that plane, not a peer product pillar equal to the Company Blueprint or the flexible coding brain. Hosted sandboxes and self-operated gVisor/Kata Jobs stay as peers behind the same contract until a decision gate says otherwise.

## What OpenShell is (context, not commitment)

NVIDIA OpenShell (Agent Toolkit, Apache 2.0; announced GTC March 2026) is a
secure runtime for autonomous agents. Relevant properties for this block:

- **Gateway** control plane: sandbox lifecycle, policy revisions, credential
providers, inference routing.
- **Supervisor** in each sandbox: out-of-process filesystem, process
(Landlock/seccomp-level), and network-egress enforcement.
- Declarative **YAML policies** (static sections locked at creation; network
rules hot-reloadable).
- Allow/deny audit export as **OCSF JSON**.
- Compute drivers: Docker, Podman, MicroVM, **Kubernetes**.
- Status at last review: **alpha (v0.0.x), explicitly “single-player” today**.

Those properties collide with specific Poseidon components — see
[Collisions](#collisions-make-these-explicit) and
[Choices / tradeoffs / options](#choices--tradeoffs--options).

## What changes

Today (`packages/sandbox/`):

- `SandboxBackend` is a two-method protocol: `provision` / `destroy`.
- The only implementation is `DockerBackend` on an internal Docker network with
*zero* egress and no CPU/memory/time caps inside the container.
- Isolation is “no credentials + no route,” not kernel/runtime hardening.

MVP must:

1. Keep the contract narrow and swappable (do not grow a Poseidon-specific
  sandbox mini-platform that only one backend can satisfy).
2. Compile **infrastructure policy** from the Company Blueprint (`stack` + role)
  into a backend-consumable form — not hand-authored per environment.
3. Enforce deny-by-default egress with an allowlist suitable for real companies
  (registry proxy returns; zero-egress PoC sandboxes are too rigid).
4. Add resource caps and stronger process/FS confinement than “trust the image.”
5. Feed sandbox allow/deny telemetry into the same audit story as the MCP
  gateway (correlated by `run_id`).
6. Run a red-team suite against whatever backend is live (egress exfil, secret
  scrape, breakout attempts) — not only against OPA semantic denies.



## What does not change

1. **Agents still hold no credentials.** Sandbox backends must not inject git
  tokens or LLM provider keys. For OpenShell specifically: configure **zero
   credential providers** for coding sandboxes so the “diff leaves only via the
   MCP Gateway” invariant stays physical.
2. **Semantic policy stays at the MCP Gateway + OPA.** Infrastructure policy
  answers “what can the process touch?”; it cannot see “which branch does
   `git.open_pr` target?” Those layers are complementary.
3. **Blueprint remains the source of truth.** OpenShell Gateway / any hosted
  control plane must not become a second org/policy authority.
4. **Orchestrator owns provision/destroy.** Temporal (MVP) / runner (PoC) calls
  `SandboxBackend`; backends do not reconcile companies or talk to the
   blueprint registry directly.
5. **Flexible coding frameworks do not own the workspace lifecycle.** Brain
  frameworks (OpenHands-class or peers) may assume they start/stop sandboxes;
   under Poseidon they must not — Hands map tools to gateway MCP only.



## Target architecture: one contract, several backends

```text
┌──────────────────────────────────────────────────────────┐
│  Intent — Company Blueprint                              │
│  stack + role + policies → infra policy IR               │
└────────────────────────────┬─────────────────────────────┘
                             │ compile (pure)
┌────────────────────────────▼─────────────────────────────┐
│  Plan — infra policy artifacts                           │
│  (OpenShell YAML / NetworkPolicy / RuntimeClass params)  │
└────────────────────────────┬─────────────────────────────┘
                             │ provision(run_id, workspace, policy)
┌────────────────────────────▼─────────────────────────────┐
│  SandboxBackend                                          │
│    ├── openshell/   preferred candidate (evaluate)       │
│    ├── hosted/      E2B / Modal-style outsourcing        │
│    ├── k8s-jobs/    self-operated gVisor/Kata (exit)     │
│    └── docker/      PoC / regression only                │
└────────────────────────────┬─────────────────────────────┘
                             │ tool server inside workspace
┌────────────────────────────▼─────────────────────────────┐
│  Law (shared with PoC) — MCP gateway proxies sandbox.*   │
│  + OPA semantic policy + audit                           │
└──────────────────────────────────────────────────────────┘
```


| Layer                    | Owns                                                                             | Must not own                                       |
| ------------------------ | -------------------------------------------------------------------------------- | -------------------------------------------------- |
| **Blueprint / compiler** | Desired infra policy (FS scope, egress allowlist, image/toolchain hints)         | Calling Docker/OpenShell APIs; live credentials    |
| **Orchestrator**         | When to provision/destroy; tying sandbox to `run_id`                             | Kernel enforcement details                         |
| **SandboxBackend impl**  | Creating the isolated workspace; applying infra policy; teardown                 | Semantic branch/path rules; company lifecycle      |
| **MCP Gateway + OPA**    | AuthN/Z, semantic allow/deny, audit of tool calls, delivery with platform tokens | Being the only egress control once hardening lands |


Suggested package shape:

```text
packages/sandbox/
  contract/        # provision(task, policy) -> workspace; destroy; (optional snapshot)
  backends/
    openshell/     # SDK/gateway; zero credential providers
    k8s-jobs/      # gVisor/Kata fallback / exit strategy
    hosted/        # E2B/Modal-style service
    docker/        # PoC path; keep until Compose regression bridge dies
```



## Relation to the other MVP building blocks


| Building block             | Relationship to sandbox hardening                                                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Company Blueprint**      | Blueprint `stack` + role compile into infra policy consumed at provision time. One truth, two enforcement layers (Rego + infra). Without the compiler, OpenShell/NetworkPolicy configs have no legitimate source.                    |
| **Flexible coding agents** | A more open Action–Observation brain *raises* the need for out-of-process FS/process/egress limits. The brain must not own sandbox lifecycle or bypass Hands→Law. Framework-native “workspace security” is defense-in-depth at most. |
| **Durable orchestrator / HITL** | Orchestrator activities call `SandboxBackend`; process-policy gates stay above this block — see [`durable-orchestrator.md`](durable-orchestrator.md).                                                                              |
| **LLM budgets**            | Orthogonal; if OpenShell inference routing exists, point it at Poseidon’s LLM gateway — budgets stay ours.                                                                                                                           |
| **Audit / KPI store**      | Sandbox infra allow/deny (e.g. OpenShell OCSF) maps into the PoC audit schema by `run_id`, alongside gateway decisions.                                                                                                              |


**Altitude check:** Blueprint answers *what is the company?*; flexible agents
answer *how does the engineer think?*; this block answers *which hardened
runtime implements* `SandboxBackend`*?* — a lower altitude, deliberately.

## Defense in depth (three layers — unchanged)


| Layer              | Question                               | PoC                                                                                 | Hardened MVP+                                                                   |
| ------------------ | -------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **Infrastructure** | What can the process physically touch? | Internal Docker networks, credential-free containers, `/workspace` path confinement | Hardened runtime (OpenShell and/or gVisor/Kata) + NetworkPolicy + resource caps |
| **Semantic**       | What does this action mean?            | MCP gateway + OPA                                                                   | Same; blueprint-compiled per-role bundles                                       |
| **Process**        | Who approved?                          | *(not in PoC)*                                                                      | Temporal gates / HITL                                                           |


A bypass of one layer must still be caught by another.

## Phase placement (proposal — open)


| Phase            | Sandbox hardening role                                                                                                                                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **PoC** *(done)* | Plain Docker; contract only. OpenShell explicitly excluded (alpha adds no exit-criterion value).                                                                                                                                                  |
| **MVP**          | Ship the contract + infra policy IR from the blueprint. **Evaluate** OpenShell (pin version, red-team). Default live backend is an explicit choice — see options below. Do not put unstable alpha on the multi-company critical path by accident. |
| **V1**           | **Decision gate:** if OpenShell is past alpha with workable multi-tenant posture, it can become the default sandbox layer and we may *never build* the self-operated gVisor node pool; otherwise `k8s-jobs/` proceeds.                            |
| **V2**           | Reassess wider use (e.g. OpenShell-wrapped agent runtimes for enterprise/on-prem); OCSF-quality feeds for SOC2-style audit deliverables.                                                                                                          |




### Exit criteria (candidates)

- **MVP:** A sandboxed agent attempting egress to a non-allowlisted host is
**blocked by the live infra layer** *and* the attempt appears in the audit
store (correlated by `run_id`). The live infra layer may be OpenShell,
NetworkPolicy+runtime, or a hosted product — but someone must enforce it.
- **V1:** The same blueprint provisions **equivalent sandbox behavior on two
backends**, proving the contract holds and OpenShell (or any favorite) stays
swappable.



## Practical split


| Concern                                      | MVP direction                                                                               |
| -------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Swapping runtimes                            | `SandboxBackend` only; keep `provision`/`destroy` narrow                                    |
| Policy source of truth                       | Company Blueprint → compiler → infra policy IR                                              |
| Semantic rules (`feature/*`, path ownership) | Still MCP gateway + OPA — never the sandbox runtime                                         |
| Credential providers in OpenShell            | Always empty for coding sandboxes                                                           |
| Who calls provision/destroy                  | Orchestrator only                                                                           |
| PoC `DockerBackend`                          | Keep as seam/regression until Compose bridge is deleted; not the production hardening story |
| Operating a gVisor/Kata node pool            | Exit strategy if OpenShell (or hosted) does not clear the V1 gate                           |


---



## Collisions (make these explicit)

These are the places this block fights other Poseidon pieces or fights itself.
None are resolved here — they are the review surface.

### C1 — Control-plane collision: OpenShell Gateway vs Company Operator

OpenShell’s Gateway wants to own sandbox lifecycle and policy revisions.
Poseidon’s Company Operator + compiler already own desired state and policy
artifacts. **Two reconcilers must not both own the same state.**

OpenShell (and any hosted control plane) must sit *behind* `SandboxBackend` as
subordinate infrastructure: orchestrator calls us; we call them; they never
read blueprints or the company registry.

### C2 — Double ownership of egress

Blueprint MVP already requires namespace NetworkPolicy (egress only to gateways
± SLIM). OpenShell Supervisor also enforces deny-by-default egress. Hosted
products have their own network rules.

Without an explicit split, three systems can author the same allowlist.
Defense in depth is fine; **two sources of truth is not.**

### C3 — Lifecycle collision: coding framework vs orchestrator vs OpenShell

Flexible-agents adoption (OpenHands-class) often assumes the framework manages
workspaces. OpenShell Gateway also manages sandboxes. Temporal/runner must own
`provision`/`destroy` for run attribution and teardown guarantees.

If the brain framework starts containers on the side, invariants and audit
correlation break.

### C4 — Flexible brain vs Supervisor restrictiveness

Open-ended coding agents expect rich shell, package installs, language servers,
sometimes Docker-ish workflows. Landlock/seccomp + locked FS + deny-by-default
egress will break “works on my laptop” framework defaults.

Hardening constrains which framework features survive. Registry-proxy egress
(blueprint hard problem: stack → sandbox) becomes mandatory sooner if the brain
needs `pip`/`npm`.

### C5 — Alpha / single-player vs multi-company Kubernetes MVP

Blueprint makes Kubernetes + namespace-per-company the deploy model. OpenShell
is still alpha and “single-player.” Treating OpenShell as the **default** MVP backend collides with “nothing young on the critical path”.

### C6 — Compiler emits OpenShell YAML with no consumer

Blueprint notes already list “sandbox / OpenShell policy YAML” as a compiler
artifact. If the live backend is hosted or plain Jobs, that YAML is dead weight
unless it is either (a) a dry-run preview of infra policy, or (b) an IR that
also projects to NetworkPolicy/RuntimeClass.

### C7 — Inference routing overlap with LLM Gateway

OpenShell can route model API calls. Poseidon’s LLM Gateway owns budgets,
metering, and attribution. Point OpenShell routing at our gateway — or disable
that surface — so budgets are not bypassed.

### C8 — Audit store is shared unfinished work

Gateway JSONL, OpenShell OCSF, flexible-agent high-volume tool traces, and
blueprint per-role attribution all want one KPI/audit store. This block must
not invent a parallel audit silo; OCSF mapping is an ingest adapter into the
schema seeded in `packages/common`.

### C9 — Scope creep: harden every agent runtime

Spec 3 rejected wrapping non-coding listener/worker pods in OpenShell for v1
(overkill; puts alpha under the whole execution plane). This block is about
**coding sandboxes**, not every agent process.

### C10 — Compose regression bridge vs K8s-only blueprint

Blueprint deletes Compose as a supported deploy path. OpenShell’s Docker driver
is attractive for local demos. Local kind/k3d + the same backend path as prod
must win; a second “OpenShell-on-Compose” story recreates dual-stack drift.

---



## Choices / tradeoffs / options

Explicit forks for review. Defaults below are **proposals**, not decisions.

### O1 — Is this a peer MVP “direction” doc or a subordinate workstream?


| Option                                               | Tradeoff                                                                    |
| ---------------------------------------------------- | --------------------------------------------------------------------------- |
| **A. Peer building block** (this doc’s existence)    | Visible; risks treating a backend choice as a product pillar                |
| **B. Fold into Blueprint** as “stack → sandbox” only | Correct altitude for policy IR; under-weights red-team / backend evaluation |
| **C. Fold into Flexible agents**                     | Wrong owner — brain must not own isolation                                  |


**Proposal:** Keep this as a third direction doc at *lower altitude*: hardening
plane + backend evaluation; policy IR owned by Blueprint compiler.

### O2 — MVP default live backend


| Option                                                               | Pros                                                                | Cons                                                                            |
| -------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| **A. OpenShell as default**                                          | Matches preferred long-term bet; exercises real Supervisor policies | Alpha on critical path; single-player vs multi-company                          |
| **B. Hosted (E2B/Modal-class) as default**                           | Fast; outsources node-pool pain                                     | Weaker alignment with compiled OpenShell YAML; vendor lock; still need contract |
| **C. K8s Jobs + NetworkPolicy (+ optional RuntimeClass) as default** | Fits blueprint K8s-only story; boring                               | We operate more surface; may rebuild what OpenShell productizes                 |
| **D. Evaluate OpenShell; default to B or C until gate passes**       | Honors “nothing young on critical path”                             | Two paths in flight briefly                                                     |


**Proposal:** **D** — evaluate/pin/red-team OpenShell in MVP; default live
backend is hosted *or* basic K8s Jobs+NetworkPolicy until the V1 gate.

### O3 — One infra-policy IR, how many projections?


| Option                                                                         | Tradeoff                                                       |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| **A. OpenShell YAML is the IR**                                                | Clean if OpenShell wins; awkward if hosted/k8s-jobs is default |
| **B. Neutral IR → project to OpenShell YAML + NetworkPolicy (+ RuntimeClass)** | One truth, two/three emissions; more compiler work             |
| **C. Hand-author NetworkPolicy and OpenShell separately**                      | Fast short-term; guaranteed drift (Collision C2)               |


**Proposal:** **B**. Outer envelope (NetworkPolicy) + inner Supervisor/runtime
policy both compiled from one allowlist/FS-scope IR.

### O4 — Who is the outer vs inner egress enforcer?


| Option                                                | Tradeoff                                                                     |
| ----------------------------------------------------- | ---------------------------------------------------------------------------- |
| **A. NetworkPolicy outer, OpenShell/runtime inner**   | Defense in depth; clear operator ownership of cluster edge                   |
| **B. OpenShell only**                                 | Simpler mental model; loses cluster-level guarantee if Gateway misconfigured |
| **C. NetworkPolicy only until OpenShell clears gate** | Honest MVP; delays Supervisor FS/process value                               |


**Proposal:** **A** when OpenShell (or equivalent) is live; **C** acceptable as
interim MVP default under O2.D.

### O5 — Contract width


| Option                                                  | Tradeoff                                                       |
| ------------------------------------------------------- | -------------------------------------------------------------- |
| **A. Keep PoC-narrow:** `provision` / `destroy` only    | Max swappability; policy passed as opaque blob/ref             |
| **B. Add** `snapshot` **/** `exec` **/ health**         | Convenient; risks baking one backend’s model into the protocol |
| **C. Pass structured** `SandboxPolicy` **in provision** | Needed for blueprint-driven hardening; still keep methods few  |


**Proposal:** **A + C** — still two lifecycle methods; provision gains an
explicit policy argument (or policy config reference) compiled from the
blueprint.

### O6 — Sequencing vs Blueprint and Flexible agents


| Option                                                         | Tradeoff                                                                                     |
| -------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| **A. Blueprint compiler (incl. infra IR) before backend pick** | Avoids C6 theater; backends consume real artifacts                                           |
| **B. Flexible agents before hardening**                        | Faster coding demos; larger blast radius if brain is open-ended on plain Docker              |
| **C. Hardening evaluate track parallel to both**               | Good; needs a clear non-gating status so alpha does not block company-converge exit criteria |
| **D. Adopt OpenHands + OpenShell in the same slice**           | Highest integration risk (C3 + C4 stacked)                                                   |


**Proposal:** **A** first or tied; **C** in parallel; avoid **D** — land Hands→Law

- orchestrator-owned lifecycle before stacking Supervisor constraints on a new
brain.



### O7 — OpenShell credential providers and inference routing


| Option                                                                      | Tradeoff                                      |
| --------------------------------------------------------------------------- | --------------------------------------------- |
| **A. Zero providers; inference routing → Poseidon LLM gateway**             | Preserves invariants and budgets              |
| **B. Use OpenShell providers for “convenience” git/LLM**                    | Destroys PoC invariants; reject               |
| **C. Disable inference routing; agents keep talking to LLM proxy as today** | Fewer moving parts; fine for coding sandboxes |


**Proposal:** **A** or **C**; never **B**.

### O8 — Audit: what must land in MVP vs later?


| Option                                                           | Tradeoff                                              |
| ---------------------------------------------------------------- | ----------------------------------------------------- |
| **A. Gateway audit only; sandbox infra denies best-effort logs** | Under-proves infra layer                              |
| **B. MVP requires infra deny visible in same audit store**       | Matches spec 3 exit idea; needs store + mapper sooner |
| **C. OCSF mapping at V1; MVP keeps per-backend logs + run_id**   | Pragmatic interim                                     |


**Proposal:** Prefer **B** as the bar; accept **C** only if the audit store is
not yet real — but then the MVP exit criterion must name the interim sink
explicitly.

### O9 — V1 decision gate criteria (what “mature enough” means)

Candidates to define before the gate, not at the gate:

- Multi-tenant / multi-company isolation story that fits namespace-per-company
- API stability compatible with pinning in our operator/Helm path
- K8s driver viability on our chosen cluster flavor (kind → prod)
- Clear subordinate mode (we own reconcile; they don’t fight the operator)
- Red-team suite green; OCSF (or equivalent) ingest proven
- Honest comparison cost vs operating `k8s-jobs/` ourselves

---



## Verdict (provisional)

Invest in **sandbox hardening as a swappable plane behind** `SandboxBackend`,
with infrastructure policy compiled from the Company Blueprint and lifecycle
owned solely by the orchestrator. Treat **OpenShell as the preferred backend
candidate to evaluate**, not as the MVP control plane and not as a third peer
pillar at the same altitude as Blueprint or Flexible Agents.

Keep hosted and self-operated gVisor/Kata paths alive until a V1 gate says we
can drop them. Do not let OpenShell replace the MCP Gateway, the operator, or
semantic policy — and do not let a coding framework or OpenShell’s own Gateway
become a second source of truth for company policy or sandbox lifecycle.

Resolve the collisions in [Collisions](#collisions-make-these-explicit) by
picking from [Choices / tradeoffs / options](#choices--tradeoffs--options)
before implementation work hard-wires a default.