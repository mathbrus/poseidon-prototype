# MVP direction: the Company Blueprint as source of truth

## Goal

The PoC proves governance for **one hardcoded agent**. The MVP must prove the
third platform invariant: **the company is declarative**. A human (or Studio
later) describes an organization once — roles, stack, workflows, policies,
budgets — and the platform materializes a governed multi-agent system from that
document.

That document is the **Company Blueprint**. It is not a prompt pack, not a Helm
chart, and not the operator. It is the **single source of truth** for *what the
company is allowed and intended to be*. Everything that follows (CRs, Rego
bundles, sandbox policies, Temporal workflow config, budget caps) is a
**compiled projection** of the blueprint.

## Runtime consequence: Kubernetes becomes required

This building block is the point where Poseidon **leaves Docker Compose as the
deployment model**.

The PoC stack (`deploy/compose/`) is enough to prove the enforcement plane for a
hardcoded org. Materializing a blueprint — hire/fire/reorg as diffs, per-company
isolation, converging live state after an edit — needs a real reconciler. Spec2
already rejected a hand-rolled Docker provisioner as reinventing Kubernetes
badly. The designed path is therefore:

**blueprint → compiler → `Company` / `AgentInstance` CRs → Company Operator**

After this step, **Kubernetes is the only supported way to run Poseidon**:
operator, namespace-per-company, NetworkPolicy egress control, and CR-driven
agent instances. Local development and testing use a small cluster (`kind`,
`k3d`, or `minikube`) on the same Helm/operator path as production — not a
second architecture.

**Do not maintain Compose and Kubernetes as dual deploy targets.** Two stacks
(networks, identity, sandbox lifecycle, operator vs script) will drift and
overcomplicate the product. Compose may linger briefly as a frozen PoC
regression bridge for E1–E4, then be deleted; it is not evolved in parallel and
is not a supported local-dev story. The MVP exit criterion for this block is:
*the operator converges a live company after a blueprint edit without manual
steps* — that criterion assumes a cluster.

## What changes

Today the “org” is a static map in the gateway (`bearer token → {agent_id,
role}`) and a fixed Rego file (`policies/poc.rego`). That is enough to prove
E1–E4; it cannot express hire/fire/reorg, per-role tool scopes, or
company-specific budgets.

MVP replaces that hardcoded org with:

1. **A versioned YAML blueprint** validated by JSON Schema.
2. **A pure compiler** that turns blueprint (+ role catalog + live registry)
   into deployable artifacts.
3. **A Kubernetes operator** that reconciles those artifacts (`Company`,
   `AgentInstance` CRs) — not a Compose-side script that launches containers.
4. **Per-role policy bundles** compiled into the same OPA input shape the PoC
   already uses: `{agent_id, role, run_id, tool, args}`.

Hire, fire, and reorg become **blueprint diffs**, not ad hoc ops scripts.

## What does not change

The blueprint never becomes an enforcement mechanism by itself:

1. **Agents still hold no credentials.** Blueprint fields may *name* which MCP
   backends a role may call; they never inject `GIT_TOKEN` or provider keys into
   agent runtimes.
2. **Side effects still leave only through gateways.** Compiling “engineer may
   `git.open_pr` on `feature/*`” produces Rego (and optionally OpenShell egress
   YAML) — it does not give the agent a remote.
3. **Semantic policy stays outside the model.** Branch rules, path ownership,
   workflow-file denial, budget hard-stops remain gateway/OPA/LLM-gateway
   concerns. Blueprint text in a system prompt is documentation, not law.
4. **The PoC enforcement plane stays the production shape.** Blueprint-compiled
   bundles target the existing decision interface; we do not invent a second
   policy engine for “declarative mode.”

Flexibility lives in *what* you declare; rigidity stays in *how* it is enforced.

## What the blueprint declares

Minimum MVP surface:

| Section | Purpose | MVP depth |
|---|---|---|
| **Company** | name, mission, product, stage | Required |
| **Stack** | languages, frameworks, repo layout, toolchain hints for sandboxes | Required (drives sandbox image / registry allowlist) |
| **Departments & roles** | which role templates, count, model tier, autonomy level | Required — start with PM, Backend Engineer, SDET (+ supervisor) |
| **Tools** | MCP backends + method-level scopes per role | Required — maps to gateway allow rules |
| **Workflows** | which SDLC processes are enabled; gate config (e.g. merge = human_approval) | Required — intake → implement → review; release/incident can wait for V1 |
| **Policies** | path ownership, branch protection, data-handling, escalation | Required — compiles to Rego (+ infra policy YAML) |
| **Budgets** | $/token caps per role, per run, per month | Required — registers with LLM gateway |

**Explicitly deferred** from the blueprint’s operative schema (V2): OASF role
records and Directory publication. The JSON Schema is the source of truth; OASF
is an export format for the marketplace later.

Abridged example (illustrative, not final schema):

```yaml
apiVersion: poseidon/v1alpha1
kind: CompanyBlueprint
metadata:
  name: hello-saas
spec:
  company:
    product: "Uptime monitoring for indie developers"
  stack:
    languages: [python]
    repos: [{ name: app, default_branch: main }]
  departments:
    engineering:
      roles:
        - template: backend-engineer
          count: 1
          model_tier: mid
        - template: sdet
          count: 1
          model_tier: mid
    product:
      roles:
        - template: pm
          count: 1
          model_tier: mid
  tools:
    backend-engineer:
      allow: [sandbox.*, git.open_pr, issues.read]
    sdet:
      allow: [sandbox.read_file, sandbox.exec, sandbox.run_tests, git.comment]
    pm:
      allow: [issues.*, git.read]
  workflows:
    implement: { enabled: true }
    review:
      enabled: true
      gates: { merge: human_approval }
  policies:
    branches:
      engineer_push: "^feature/[a-z0-9-]+$"
      deny: [main, master]
    paths:
      deny_on_pr: [".github/workflows/**"]
      ownership:
        - path: "src/**"
          role: backend-engineer
  budgets:
    per_run:
      implement: { usd: 15 }
```

## Target architecture: declare → compile → reconcile → enforce

```text
┌──────────────────────────────────────────────────────────┐
│  Intent — Company Blueprint (YAML + JSON Schema)         │
│  (roles, stack, workflows, policies, budgets)            │
└────────────────────────────┬─────────────────────────────┘
                             │ compile (pure, diffable)
┌────────────────────────────▼─────────────────────────────┐
│  Plan — artifacts                                        │
│  • Company / AgentInstance CRs                           │
│  • per-role OPA/Rego bundles                             │
│  • sandbox / OpenShell policy YAML (infra layer)         │
│  • budget registrations + workflow gate config           │
└────────────────────────────┬─────────────────────────────┘
                             │ reconcile on Kubernetes
┌────────────────────────────▼─────────────────────────────┐
│  Reality — Company Operator + Registry                   │
│  (namespace, listeners/workers, NetworkPolicy, identity) │
└────────────────────────────┬─────────────────────────────┘
                             │ runtime calls
┌────────────────────────────▼─────────────────────────────┐
│  Law — MCP gateway + OPA + LLM gateway + Temporal gates  │
│  (same chokepoints as the PoC, now blueprint-driven)     │
└──────────────────────────────────────────────────────────┘
```

| Layer | Owns | Must not own |
|---|---|---|
| **Intent (blueprint)** | Desired company shape; human-reviewable diffs | Live credentials, runtime loop, “hope the model obeys” |
| **Plan (compiler)** | Validation, template resolution, artifact emission, dry-run diffs | Side effects on the cluster (stay pure) |
| **Reality (operator + registry)** | Convergence to CRs on Kubernetes; instance/status; drain rules later | Being the policy source of truth (that stays the blueprint) |
| **Law (gateways + OPA + Temporal)** | Allow/deny, audit, budgets, HITL | Parsing free-form blueprint prose at request time |

## Compiler responsibilities

The compiler is a **pure function**:
`(blueprint, role_catalog, registry_snapshot) → deployment_plan`.

It must:

- Validate schema + cross-checks (every workflow node has a hired role; every
  tool scope names a registered MCP backend; budgets cover enabled workflows).
- Resolve role templates → concrete agent specs (prompt/skills binding, tool
  allow-list, model tier, group topics to join).
- Emit **diffable** plans against the registry (add/remove/update agent
  instances, replace policy bundles, resize budgets).
- Produce **two enforcement projections** from one source: Rego (semantic) and
  sandbox/OpenShell policy YAML (infrastructure) — one truth, two layers.

It must not:

- Shell out to Docker or mutate cluster state (the operator’s job).
- Embed platform secrets into agent specs.
- Treat OASF/Directory as required for MVP compile success.

**Preview before apply** is part of the MVP surface (CLI/API first): show plan +
projected cost, then apply.

## Operator & reconciliation

- Compile blueprint → **`Company` + `AgentInstance` CRs** (and ConfigMap /
  Secret *references* for policy bundles — not raw git tokens in the blueprint).
- Let a **Company Operator** reconcile on the cluster: namespace-per-company,
  NetworkPolicies (egress only to gateways ± SLIM), hybrid listeners/workers,
  policy install at the gateway.
- Keep AGNTCY Identity badges off the MVP critical path; internal workload
  identity / JWT at gateways is enough (badges at V1).

## Hard problems the blueprint design must acknowledge

1. **Blueprint vs in-flight runs.** A reorg mid-`implement` needs drain policy
   (finish / reassign / abort) — declare defaults per workflow, don’t improvise
   in the operator.
2. **Path ownership as semantic policy.** Ownership maps only matter if the
   gateway sees changed paths (the PoC bundle trick already enables this).
3. **Autonomy is data.** MVP can expose gate flags / autonomy level; later
   “earned autonomy” should mutate those via controlled overlays, not via the
   model granting itself power.
4. **Stack → sandbox.** Blueprint `stack` must compile into toolchain/image and
   egress allowlist (registry proxy returns at MVP). Zero-egress PoC sandboxes
   are too rigid for arbitrary companies.
5. **Role catalog vs instance.** Templates live in a versioned catalog; the
   blueprint *instantiates* them. Instances are registry rows, not Directory
   citizens (V2).

## Relation to other MVP building blocks

| Building block | Relationship to blueprint |
|---|---|
| **Flexible coding agents** | Blueprint selects role template + model tier + tool scope; the brain framework is an implementation detail of the engineer role, still bound by compiled law |
| **Standing and ephemeral agents** | Blueprint extends with crew templates + `may_spawn`; standing roles remain CR-reconciled, ephemeral crews are run-scoped — see [`standing-and-ephemeral-agents.md`](standing-and-ephemeral-agents.md) |
| **Durable orchestrator / HITL** | Blueprint `workflows.*.gates` configures which signals exist; orchestrator owns durability — see [`durable-orchestrator.md`](durable-orchestrator.md) |
| **LLM budgets** | Blueprint `budgets` → LLM gateway hard-stops |
| **Sandbox hardening** | Blueprint `stack` + role → infra policy IR behind `SandboxBackend`; OpenShell is a preferred backend candidate — see [`sandbox-hardening.md`](sandbox-hardening.md) |
| **SLIM review** | Blueprint enables `review` and which roles join the group session |

## Practical split

| Concern | MVP direction |
|---|---|
| Source of truth for org shape | Company Blueprint (YAML + JSON Schema), Git-versioned |
| Materialization | Compiler → CRs + policy/budget artifacts; Operator reconciles **on Kubernetes** |
| Platform deploy model after this step | **Kubernetes only** (incl. local via kind/k3d/minikube); no dual Compose support |
| Policy evaluation | Still OPA at MCP gateway; bundles compiled per role |
| Infra sandbox policy | Compiled from same blueprint; never a second source of truth |
| PoC hardcoded identity + `poc.rego` | Keep as governance proof; treat as the seam the first compiled company replaces |
| OASF / Directory / authoring copilot | Out of MVP operative path |

## Verdict

For MVP, invest in the **Company Blueprint** as the declarative control surface:
a reviewable YAML document that compiles into Kubernetes desired state and into
the same gateway policy interface the PoC already enforces. Adopting this block
means **deploying Poseidon on Kubernetes only** — the operator and CR pipeline
are how blueprint diffs become a live company. Local and remote share that one
runtime; Compose is not kept as a parallel platform. Change the company by
changing the blueprint; the operator converges; the gateways still refuse
anything the blueprint did not authorize.
