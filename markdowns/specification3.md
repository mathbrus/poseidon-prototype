# Prompt after Specification 2

"Now challenge that plan again by looking at another aspect : where could a tool like NVIDIA OoenShell help make the plan more solid ? Does it fit ? Can it allow us to build on top of it ? Does it integrate in the long term plan ? Propose an adapter plan if needed, or argue why it’s not needed"

# Challenge — Does NVIDIA OpenShell belong in this architecture?

First, what it is (verified against current docs): **NVIDIA OpenShell** (announced GTC March 2026, Apache 2.0, part of the NVIDIA Agent Toolkit) is a *secure runtime for autonomous agents*. Each agent runs in a sandbox with **out-of-process policy enforcement**: a **Gateway** control plane owns sandbox lifecycle, policy revisions, credential providers, and inference routing; a **Supervisor** inside each sandbox enforces filesystem, process (Landlock/seccomp-level), and network-egress policy on the agent as a restricted child process. Policies are declarative YAML — static sections locked at creation, network rules hot-reloadable — with a full allow/deny audit trail exportable as OCSF JSON. Compute drivers: Docker, Podman, MicroVM, **Kubernetes**. Status: **alpha (v0.0.x), explicitly "single-player" today**.

That description collides head-on with specific parts of the plan, so component by component:

## Overlap map

| Our component | OpenShell equivalent | Assessment |
|---|---|---|
| **Sandbox pool** (K8s Jobs + gVisor/Kata + egress NetworkPolicy + registry proxy) | Sandboxed execution with deny-by-default egress, policy-enforced process/filesystem limits, audit trail — *its exact design center* | **Strongest fit.** This was ranked our #1 difficulty, and OpenShell is that component productized by a vendor with a security-partner ecosystem |
| **MCP Gateway** (semantic policy: "push to `feature/*`, never `main`") | Gateway evaluates actions at *binary / destination / method / path* level | **Not a replacement.** OpenShell sees "HTTPS to github.com"; it cannot see *which branch* an MCP `git.push` targets. Infrastructure-layer vs application-layer policy — complementary, not competing |
| **LLM Gateway** (budgets, metering, run attribution) | "Inference routing" redirects model API calls to chosen backends | **Point it at our LLM Gateway.** Routing overlaps; budgets/attribution don't exist there |
| **Agent listener/worker pods** (namespace + NetworkPolicy isolation) | Could run every agent inside an OpenShell sandbox | **Overkill for v1.** Non-coding agents don't execute arbitrary code; K8s-native isolation is boring and sufficient. Putting alpha software under the entire execution plane inverts the "nothing young on the critical path" rule |
| **Company Operator / compiler** (policy source of truth, lifecycle) | OpenShell Gateway also owns policy revisions and sandbox lifecycle | **Control-plane collision risk.** Two reconcilers must not both own state. OpenShell must be subordinate: a *driver* our orchestrator calls, consuming policy YAML our compiler generates |
| **KPI / audit store** | OCSF JSON audit export of every allow/deny | **Clean feed.** OCSF is a standard; ingest it alongside gateway decisions |

## Where it genuinely makes the plan more solid

1. **It attacks our top-ranked risk.** The hardest, most dangerous build item was sandbox security (container escape, egress exfiltration, secret leakage) and the plan committed us to *operating* a gVisor/Kata node pool. OpenShell outsources kernel-level enforcement, live policy updates, and audit to a maintained project — on Kubernetes, via its K8s compute driver.
2. **It hardens the credential invariant instead of weakening it.** OpenShell normally *injects* credentials via providers; we simply configure **zero providers** for code sandboxes. Our invariant (diff leaves only via the MCP Gateway) stays, and OpenShell's deny-by-default egress now *enforces* that nothing else can leak — previously that depended solely on our own NetworkPolicy discipline.
3. **Defense in depth becomes explicit and layered:** OpenShell enforces *infrastructure* policy (what the process can touch), the MCP Gateway enforces *semantic* policy (what the action means), Temporal gates enforce *process* policy (who approved). Three independent layers; a bypass of one is caught by another.
4. **Strategic alignment, not friction.** Cisco — AGNTCY's founding force — is an announced OpenShell security partner for runtime policy alignment. AGNTCY (identity, messaging, discovery) and OpenShell (runtime enforcement) are complementary layers of the same emerging stack; adopting both is coherent, not a bet on rival ecosystems.

## Why not build *on top of* it

Same verdict shape as CrewAI: it's a component, not a foundation. OpenShell has no org semantics, no durable orchestration, no budgets, no semantic tool policy, no multi-agent communication — the platform remains ours. And two hard cautions: it is **alpha with unstable APIs and a single-operator posture**, so it cannot be a hard dependency yet; and its Gateway wants to own policy/lifecycle state, so it must sit *behind* our operator/orchestrator as one pluggable backend, never as the source of truth.

---

# Adapted plan (small delta)

**1. OpenShell becomes a sandbox backend — the interface we already built for this:**

```text
packages/sandbox/
  contract/        # provision(task, policy) -> workspace; destroy; snapshot
  backends/
    openshell/     # via OpenShell SDK/gateway (K8s or Docker driver); zero credential providers
    k8s-jobs/      # self-operated gVisor/Kata path (fallback / exit strategy)
    hosted/        # E2B/Modal-style service
```

**2. Compiler emits one more artifact:** per-role **OpenShell policy YAML** (filesystem scope, egress allowlist = registry proxy + MCP Gateway only) generated from the same blueprint that produces Rego bundles. One source of truth, two enforcement layers.

**3. Orchestrator provisions sandboxes through the backend contract;** OpenShell's gateway runs as subordinate infrastructure in the platform namespace — it never talks to blueprints or the registry directly.

**4. Audit:** ingest OpenShell's OCSF allow/deny records into the KPI/audit store, correlated by run ID with MCP Gateway decisions.

**5. Phase placement:**

| Phase | OpenShell role |
|---|---|
| **POC** | Optional local sandbox via Docker driver (it's genuinely two commands); plain container fallback kept — alpha software must not block the PR-under-policy demo |
| **MVP** | Candidate default backend alongside hosted service; pin version, run the red-team suite against it |
| **V1** | **Decision gate:** if matured past alpha with multi-tenant support, it becomes the default sandbox layer and we *never build* the self-operated gVisor node pool; otherwise `k8s-jobs/` proceeds as planned |
| **V2** | Reassess wider use: OpenShell-wrapped agent runtimes for enterprise/on-prem tenants; OCSF export feeding SOC2 audit deliverables |

**Exit-criteria additions:** MVP — a sandboxed agent attempting egress to a non-allowlisted host is blocked by OpenShell *and* the attempt appears in the audit store; V1 — the same blueprint provisions identical sandbox behavior on two different backends (proving the contract holds and OpenShell stays swappable).

**Verdict:** OpenShell fits — better than any tool challenged so far — because it lands exactly on the plan's highest-risk, least-differentiated component. Adopt it as the preferred sandbox backend behind the existing pluggable contract, generate its policies from the blueprint, and keep the self-operated path alive as the exit strategy until it leaves alpha. It does not replace the MCP Gateway, the orchestrator, or the control plane — and must not.