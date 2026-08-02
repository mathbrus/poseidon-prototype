# Poseidon MVP — implementation plan

**Status:** working plan. Derived from `mvp-overview.md` (the binding design)
and the PoC code as it exists in this repository today. Everything under
`markdowns/poc/`, the root `README.md`, and the draft documents directly under
`markdowns/MVP/` is historical context only and imposes nothing here. A
review pass on sequencing is folded in: the transitional-roster bridge and
delivery-gating calls are sequencing choices 6–8 (§2), previously step-local
engineering decisions are frozen contracts (§2b), and the two
milestone-sized steps are split into landable slices (steps 1, 16).

**How to read this plan.** The work is broken into **25 steps** across four
phases that map 1:1 onto the overview's milestones M1–M4 (§9), plus a final
acceptance step and a small "anytime" track. Two steps (1 and 16) were
milestone-sized phases disguised as steps and are split into separately
landable slices (1a–1d, 16a–16d); references to "step 1" or "step 16"
elsewhere mean all slices complete. Each step names its dependencies, the
concrete changes (packages, files), what gets **deleted** when it lands (per
the no-backwards-compatibility stance), and a verifiable **done-when**.
Steps within a phase can run in parallel wherever the dependency lines
allow — the graph is summarized in [§7](#7-dependency-graph). Mechanisms
that many steps assume are frozen up front in
[§2b](#2b-architecture-contracts-frozen-up-front), never decided inside a
step body.

References like §4.2 or O-1 point into `mvp-overview.md`.

---

## 1. Starting point — what exists, what doesn't

Current code (verified against the tree, not the docs):

| Exists today | State |
| --- | --- |
| `packages/common` | `AuditEvent` v1 (10 fields, JSONL sink), `new_run_id()` (ULID), JSON-stdout telemetry with an explicit OTel seam |
| `packages/gateway` | FastMCP app on :8080; pipeline authenticate (static bearer → `identity.py` map) → OPA (`policies/poc.rego`) → audit → execute; tools: `sandbox.{read_file,write_file,list_dir,exec,run_tests,git_bundle}`, `repo.archive`, `git.open_pr` (bundle-apply gives OPA the changed paths); sandbox registration is an **in-memory dict** behind `POST /admin/sandboxes` |
| `packages/runner` | `poseidon-poc` Typer CLI; `flow.py` with five step functions `create_run → fetch_workspace → provision_sandbox → dispatch_task → teardown`; run state in `runs/<id>/manifest.json` |
| `packages/agent` | LangGraph brain (plan → act ⇄ verify → deliver) behind FastAPI (`POST /task`); toolbox maps model tools to gateway MCP (injects `run_id`, composes bundle + `open_pr`); talks to LiteLLM proxy anonymously |
| `packages/sandbox` | `SandboxBackend` protocol v1 (`provision(run_id, workspace_tar)` / `destroy`), Docker backend on `sandboxnet`, FastAPI toolserver (files/exec/tests/bundle) |
| `policies/` | `poc.rego` + tests: default-deny, feature-branch rule, `.github/workflows/**` path deny, run-assignment check |
| `deploy/compose/` | Four internal Docker networks; secrets split (GIT_TOKEN → gateway, LLM_API_KEY → LiteLLM, agent/sandbox hold nothing); zero-egress sandbox |
| `tests/` | Unit suites per package + E1–E4 e2e exit-criteria scripts against Compose |

Does not exist in any form: Kubernetes/Helm deployment, Temporal, Postgres,
`packages/schema`, `packages/compiler`, `packages/operator`,
`packages/registry`, `packages/gateway-llm`, `crds/`, blueprint or role
catalog, crew machinery, ephemeral identities, issue/review/merge gateway
tools, egress proxy, budgets/metering, workload identity, any PM agent.

---

## 2. Sequencing choices this plan makes

§9 fixes the milestone order; a few finer-grained calls are left implicit in
the overview. This plan resolves them as follows (flagging each so review can
overturn):

1. **Audit sink migration is two-staged.** §4.10 says "the JSONL file retires
   with Compose" (end of M1), but the Postgres sink lives in the registry,
   which is an M2 deliverable. Resolution: JSONL moves to a PVC at M1 (step
   1), the sink switches to registry Postgres when the registry lands (step
   8), and schema v2 + the three new event sources land in Phase 3 (step 13).
2. **Sandbox contract v2 lands at M1 with hand-authored policy.**
   `provision(run_id, tar, policy)` needs `SandboxPolicy` before the compiler
   exists. Step 4 introduces the model with static config values; step 12
   swaps in the compiler-emitted infra IR. The method set never changes twice.
3. **Run records stay manifest files through M1.** The registry `runs` table
   replaces them at step 8. Same for the gateway's in-memory sandbox
   registration (resolution row 16 of §11): tolerated through M1, replaced at
   step 8.
4. **The platform verify gate lands with the implement-workflow reshape**
   (step 19, Phase 3) rather than waiting for the brain swap. §9 lists it
   under M4, but it is brain-independent: the activity runs
   `sandbox.run_tests` via the gateway regardless of which brain claimed
   completion. The brain-side verify removal happens at brain adoption (step
   22).
5. **A PM agent runtime step is added** (step 17). The overview requires a
   standing PM that scopes goals, writes tickets, and calls `crew.request`,
   but no draft assigns its implementation anywhere. Nothing in the PoC plays
   this role.
6. **A transitional standing `backend-engineer` bridges M2→M3.** Without it,
   the lean apply at step 10 orphans implement and the step-12 governance
   regression: after "PM only" there is no principal that may call
   `git.open_pr` until crew fulfillment lands at step 16/19 — implement would
   be dead for half a phase and the E2-class deny/recover test would have no
   caller. So `hello-saas` carries a standing `backend-engineer` (running the
   PoC brain) from step 7 through step 19, where a blueprint diff **fires
   it** — deliberately, as a live demo of hire/fire-as-diff and the drain
   default. This is a scheduled bridge with a deletion point (like
   `poc.rego`), not a frozen fallback; the lean-roster exit assertions are
   claimed at steps 19/25, not at step 10 (overview §9 M2/M3).
7. **The lean-roster compiler checks land in two stages.** The general checks
   (workflow node → hired standing role, known crew templates, explicit
   member allow-lists, no ⊆-parent) land at step 9. The strict roster stance
   — *no standing executor counts; workflow nodes bind to spawners only* —
   becomes a compile **error** at step 19, together with the firing diff.
   Enforcing it at step 9 would make the bridge blueprint (choice 6)
   uncompilable.
8. **The PoC brain's deliver step becomes dispatch-gated at step 19, and its
   verify becomes advisory there too** — not at the brain swap (step 22).
   The reshaped implement workflow owns verify-then-deliver (overview §4.3,
   §6.2); a brain that self-delivers on green tests would race the platform
   gate during the overlap. Since we control the PoC graph, gating its
   deliver node behind a workflow dispatch is a small change and belongs to
   the workflow reshape, not the brain adoption.

---

## 2b. Architecture contracts, frozen up front

These four mechanisms were previously "decide inside the step". They are not
open product decisions like O-1…O-5 — they are engineering contracts that
many later steps assume, and leaving them inside step bodies made the
"independently landable steps" claim weaker than it looked. Each now has a
chosen default, overturnable in review like the §2 choices, never improvised
mid-step:

| Contract | Default | Consumed by |
| --- | --- | --- |
| **Egress → run attribution** | A per-run proxy credential injected into the sandbox environment at provision time; the proxy maps credential → `run_id` on every allow/deny event. Chosen over source-IP maps (fragile across pod restarts) and bare HTTP headers (spoofable from inside the sandbox). Affects sandbox provision, the audit schema, and nothing else. | steps 4, 5, 13, 23 |
| **Ephemeral identity mint/revoke** | Platform-signed short-TTL JWTs minted by orchestrator fulfill activities; both gateways verify the signature **and** the live `run_participants` row; revocation = row termination, effective before TTL (the kill-worker criterion). No per-run Kubernetes service accounts (overview §4.9). | steps 11, 16a, 16d, 12 |
| **Compiled-bundle delivery to OPA** | The operator persists compiled bundles (via the registry / object storage) and serves them over the standard OPA bundle API; OPA polls. **No runtime OPA data pushes anywhere:** run binding rides in the decision input, enriched by the gateway from the registry (overview §5.1). This replaces the PoC's `data.runs.assignments` and is what makes gateway/OPA restarts safe. | steps 10, 12, 16b |
| **Budget hard-stop signaling** | Enforcement is local at the LLM gateway (block further calls + ledger + audit event). The workflow observes: dispatched brains surface a distinguishable budget error, and activities check ledger headroom at their boundaries. **No Temporal client inside the LLM gateway** (overview §4.8). | steps 14, 19 |

---

## 3. Phase 1 — M1: the PoC, re-hosted and durable

Goal: E1–E4 equivalents pass on Kubernetes, plus kill-the-worker
crash-resume. Then Compose and the Docker sandbox backend are deleted.
Nothing in this phase depends on OpenHands, OpenShell, or SLIM (§9 hard
rule).

### Step 1 — Helm + kind: re-host the existing PoC services (four slices)

Previously one step; it was a milestone slice disguised as one landable unit
(chart structure, namespaces, secrets, NetworkPolicy, PVC audit, four service
re-hosts, and an e2e pass), which hides risk and makes "done when" ambiguous
under partial landings. Split into four separately landable slices;
downstream references to "step 1" mean 1a–1d complete.

**Step 1a — chart skeleton, namespaces, secrets.** *Depends on: nothing.*
`deploy/helm/poseidon/` (one chart or umbrella) creating the **platform
namespace** and a **company namespace** (hardcoded `hello-saas` until the
operator exists); Kubernetes Secrets layout: `GIT_TOKEN` mountable only into
the gateway, `LLM_API_KEY` only into LiteLLM; static bearer tokens stay for
now. `kind` cluster bootstrap script (`deploy/kind/` or a Makefile target) +
`kind load docker-image` plumbing. Same chart and values structure for a real
cluster — parity is exit criterion 7 and starts here.
*Done when:* the chart installs cleanly on a fresh kind cluster; secrets
exist only in their designated namespaces.

**Step 1b — service re-host.** *Depends on: 1a.* OPA, LiteLLM (`llm-proxy`),
and the MCP gateway into the platform namespace; the agent Deployment into
the company namespace; audit JSONL path onto a PVC mounted by the gateway
(sequencing choice 1). The runner stays a host-side CLI, reaching the
gateway/agent via port-forward or NodePort.
*Done when:* every service is Ready on kind and a manual gateway tool call
round-trips.

**Step 1c — NetworkPolicy hardening.** *Depends on: 1b.* Deny-all default in
the company namespace; agent egress only to the MCP gateway and LLM proxy
services; gateway egress to OPA and the external git host. This replaces the
four Compose networks.
*Done when:* an in-pod probe confirms the policy (agent cannot reach anything
but the gateways); no pod in the company namespace has a credential-shaped
env var.

**Step 1d — E1 on kind.** *Depends on: 1c.* The E1 happy path scripted
against the kind cluster.
*Done when:* E1 is green on kind.

> **Status (2026-08-01): done, all four slices** — chart under
> `deploy/helm/poseidon/`, kind bootstrap under `deploy/kind/`, E1 suite in
> `tests/e2e_kind/` (marker `e2e_kind`). Keep an eye on: the company
> namespace is chart-hardcoded until the operator (step 10); NetworkPolicy
> enforcement depends on the bootstrap-installed Calico (kind's default CNI
> enforces nothing); the gateway/agent NodePorts ride kind
> extraPortMappings fixed at cluster creation; audit JSONL sits on the
> gateway PVC until step 8. Since 2026-08-02 e2e runs use the deterministic
> mock LLM by default (`packages/mockllm` impersonating llm-proxy;
> `REAL_LLM=1` restores the paid brain) — new e2e scenarios need a script in
> its brain, not a provider key.

### Step 2 — Self-hosted Temporal on the same Helm path

**Depends on:** step 1.

**Build:**
- Temporal server, version-pinned, in the platform namespace via its Helm
  chart (or rendered manifests under `deploy/helm/`), with Postgres as its
  persistence store — this is the first Postgres in the stack and can later
  host the registry database (step 8).
- Runnable on kind with the same values path as production. No Temporal Cloud
  anywhere (§4.2, O3 = B).

**Done when:** a smoke-test worker connects, runs a trivial workflow, and the
workflow history survives a Temporal pod restart.

> **Status (2026-08-01): done** — official Temporal chart, version pinned in
> `deploy/kind/kind-up.sh` (`TEMPORAL_CHART_VERSION`), values in
> `deploy/helm/temporal-values.yaml`, persisted in the poseidon chart's
> Postgres (StatefulSet, superuser `poseidon`). Keep an eye on: the schema
> hook jobs assume Postgres is Ready before the temporal release installs
> (kind-up orders this); the same instance is earmarked to host the registry
> database at step 8.

### Step 3 — `packages/runner` → `packages/orchestrator`: durable implement workflow

**Depends on:** steps 1–2.

**Build:**
- Rename the package; adopt the §4.2 layout: `cli/`, `workflows/`,
  `activities/`, `gates/`, `worker/`.
- Port the five `flow.py` steps to activities behind a thin engine adapter
  (the step-shaped functions survive; Temporal stays swappable in principle):
  `create_run`, `fetch_workspace`, `provision_sandbox`, `dispatch_work`,
  `teardown`. Activities remain thin clients over the gateway client,
  `SandboxBackend`, and agent HTTP. **No `GIT_TOKEN`, no provider keys in the
  worker** — the orchestrator bearer token (successor of the runner token) is
  its only credential and is treated as privileged in policy.
- `implement` workflow: activity sequencing, per-activity timeouts and
  retries (replacing `step_timeout_s` / polling loops), and **guaranteed
  compensation** — teardown runs on success, failure, cancel, timeout, and
  worker death (§6.4 rows 1–3).
- `gates/` ships as a real **skeleton** at M1 — a generic signal handler, a
  timer/escalation primitive, and a `process-gate` audit-event stub — with
  **no gate wired into implement**. M1 implement is gate-free by design
  (overview §9 M1, amended from the earlier "hardcoded gates" phrase, which
  named machinery the sequence never built); step 20 configures gates from
  the blueprint instead of inventing the machinery then. If process gates
  are ever the sole owner of a guarantee (O-1 A on merge), the framework
  deserves an early, explicit spine — this is it.
- Run identity: workflow id derived deterministically from `run_id`
  (`implement-<run_id>`, §5.5). Run manifests stay file-based in this step
  (written from activities; replaced at step 8).
- CLI becomes `poseidon run|signal|status` — a thin Temporal client. The
  `poseidon-poc` entry point is deleted.
- Worker Deployment in the platform namespace, added to the Helm chart.

**Done when:** implement runs end-to-end on kind through Temporal; killing
the worker pod mid-`implement` and restarting it resumes or compensates the
workflow with no leaked sandbox and an honest terminal run record.

> **Status (2026-08-02): done** — E1 doubles as the kill-the-worker proof
> (the suite kills the worker pod mid-run). Keep an eye on: the Dockerfiles
> still `pip install` latest deps while the host follows `uv.lock` —
> `orchestrator/clients.py` carries mcp version-compat shims until the
> lockfile-pinned builds land at step 6; run manifests live on a
> ReadWriteOnce PVC (hence the worker Deployment's Recreate strategy) until
> the registry replaces them at step 8.

### Step 4 — Sandbox contract v2 + `k8s_jobs` backend

**Depends on:** step 1. Parallel with steps 2–3; integrates with step 3's
`provision_sandbox` activity once both land.

**Build:**
- `packages/sandbox/contract.py` v2 (§5.3):

  ```python
  class SandboxBackend(Protocol):
      def provision(self, run_id: str, workspace_tar: bytes,
                    policy: SandboxPolicy) -> SandboxHandle: ...
      def destroy(self, handle: SandboxHandle) -> None: ...
  ```

  `SandboxPolicy`: image/toolchain, egress allowlist, filesystem scope,
  CPU/memory/disk caps, wall-clock deadline. `SandboxHandle` gains
  namespace + Job/pod reference in place of `container_id`. Values are
  hand-authored config at this step (sequencing choice 2).
- `packages/sandbox/backends/k8s_jobs/`: Kubernetes Job per sandbox —
  resource limits and `activeDeadlineSeconds` from the policy, toolserver
  image, workspace tar delivered at startup, `run_id` labels, addressable by
  the gateway (headless Service or pod IP), NetworkPolicy allowing ingress
  only from the gateway and egress only to the egress proxy (step 5).
  Only the orchestrator calls provision/destroy (§4.6).
- Toolserver role unchanged (files, exec, tests, bundle; gateway-only
  ingress).

**Done when:** provision/destroy round-trips on kind; `sandbox.*` calls proxy
through the gateway to the Job's toolserver; a runaway workload is killed by
the caps/deadline (this is part of the §4.6 exit criteria).

> **Status (2026-08-02): done** — E1 plus `tests/e2e_kind/test_sandbox_k8s.py`
> (round-trip + deadline kill) green on kind. Keep an eye on: the worker
> Role needs **both** `create` and `get` on `pods/exec` — the Python
> client's exec is a WebSocket GET handshake, unlike kubectl's POST; the
> workspace tar rides that exec API with `head -c <n>` standing in for EOF
> (the ws client cannot half-close stdin); `SandboxPolicy` values are
> hand-authored until step 12, and `SandboxPolicy.env` is the reserved §2b
> seam for step 5's per-run proxy credential; the Docker backend and its
> tests stay in-tree only for step 6 to delete. Operational scar: removing a
> Docker network the kind node had joined orphaned the kubelet's registered
> node IP (apiserver→kubelet wedged; fixed by restarting the node
> container) — only relevant to clusters created before this step.

### Step 5 — Egress proxy for sandboxes

**Depends on:** step 1. Pairs with step 4.

**Build:**
- A proxy Deployment in the platform namespace enforcing a per-run host
  allowlist (static config in this phase, e.g. PyPI/npm hosts; compiled from
  the blueprint at step 12). Blueprints whose stack needs nothing get an
  empty allowlist — zero egress as before.
- **Every deny emits an audit event** (`source=sandbox-infra`) carrying
  `run_id` — this is what makes the "egress attempt blocked *and* visible in
  the audit store" exit criterion mechanically satisfiable (§4.6).
  Attribution uses the frozen §2b contract: a per-run proxy credential
  injected into the sandbox env at provision time (step 4 provides the
  injection point); the proxy maps credential → `run_id` on every event.
- Sandbox NetworkPolicy allows egress **only** to the proxy (plus cluster
  DNS); NetworkPolicy remains the silent backstop beneath it.

**Done when:** from inside a sandbox, a request to a non-allowlisted host is
blocked and a correlated deny event lands in the audit sink; an allowlisted
`pip install` succeeds.

### Step 6 — M1 acceptance suite + deletions

**Depends on:** steps 3, 4, 5.

**Build:**
- Port E1–E4 to the cluster: happy path (E1), injection denied + recovery
  (E2), egress containment adapted to NetworkPolicy + proxy (E3 — agent pod
  reaches only the gateways; sandbox reaches only the proxy; secret scan of
  pod specs), audit-story reconstruction (E4).
- Add the M1-specific criterion: kill-the-worker crash-resume (from step 3)
  as a scripted test.
- Rewire CI: unit + OPA jobs unchanged; image builds and e2e move to a
  kind-based job.
- **Lockfile-pinned image builds:** the Dockerfiles stop `pip install`-ing
  packages at whatever version is current and instead install from `uv.lock`
  (e.g. `uv export`/`uv sync` in the build), so images and the host
  environment resolve identical dependency versions. Motivated by a real
  step-3 incident: images picked up an `mcp` release with breaking API
  renames (`streamable_http_client` 3-tuple → 2-tuple, `isError` →
  `is_error`) that the lockfile-pinned host never saw.

**Delete:** `deploy/compose/`, `packages/sandbox/.../backends/docker.py` and
its tests, Compose-specific env documentation, any `poseidon-poc` remnants,
and the mcp version-compat shims in `orchestrator/clients.py` (the tuple
unpack and `is_error`/`isError` fallbacks — dead once every environment
resolves the same locked `mcp`).

**Done when:** the suite is green on kind in CI and Compose plus the Docker
backend are gone from the tree.

---

## 4. Phase 2 — M2: the declarative company

Goal: `hello-saas.yaml` → compile → preview → apply → a converged live
company; compiled per-role bundles and workload-identity JWTs replace
`poc.rego` and the static token map, which are then deleted.

### Step 7 — `packages/schema`: blueprint schema + role-template catalog

**Depends on:** nothing (can start during Phase 1).

**Build:**
- JSON Schema for the eight blueprint sections (§4.1): `company`, `stack`,
  `departments.roles`, `crews`, `tools`, `workflows` (+ gate config + drain
  defaults), `policies`, `budgets`.
- Versioned role-template catalog: `pm`, `backend-engineer`, `sdet`,
  `analyst` (placeholder, valid only at `count: 0`). Templates carry tool
  scopes, model tier, and prompt/runtime hints. In the MVP end state,
  `backend-engineer` and `sdet` exist **only** as crew-member material — the
  schema does not forbid standing counts structurally, the compiler error
  does, and that error lands at step 19 with the firing diff (sequencing
  choice 7), not here.
- Crew templates: `feature-delivery` (planner + one executor on the
  `backend-engineer` template — planner allow-list per the overview §4.4
  planner contract: `sandbox.read_file`, `sandbox.list_dir`, `issues.read`,
  no write tools) and `feature-review` (one reviewer on `sdet`), each member
  with an explicit tool allow-list and budget inheritance.
- `examples/hello-saas.yaml` in its **bridge profile** (sequencing choice 6):
  PM `count: 1`, **transitional `backend-engineer count: 1`** (runs the PoC
  brain, keeps implement dispatchable and gives the step-12 governance
  regression its caller), `analyst count: 0`, both crews,
  `review.gates.merge = human_approval`. Step 19's blueprint diff removes
  the transitional engineer.
- Schema validation tests with pointful error output.

**Done when:** `hello-saas.yaml` validates; representative invalid documents
fail with actionable errors.

### Step 8 — `packages/registry`: Postgres models + API; runs and registrations move in

**Depends on:** step 3 (orchestrator writes runs); parallel with step 7.

**Build:**
- Registry database (own database; may share the Postgres instance deployed
  for Temporal in step 2) with the §4.1 tables: `companies`,
  `agent_instances`, `runs`, `run_participants`, `budget_ledger`,
  `sandbox_registrations` — plus `audit_events` (sequencing choice 1) and
  `crew_requests` (consumed at step 16).
- Thin API service or shared library in the platform namespace. Write
  ownership enforced as designed: operator → instances, orchestrator → runs
  and participants, LLM gateway → ledger, nothing else writes.
- Orchestrator `create_run` / `record_artifacts` / `teardown` write `runs`
  rows (workflow-id mapping, ticket/PR URLs, terminal status).
- Gateway sandbox registration becomes registry-backed and restart-safe
  (resolution row 16).
- Audit sink switches from JSONL to the `audit_events` table (schema still
  v1; v2 at step 13).

**Delete:** `runs/<id>/manifest.json` writing and the `Manifest` model; the
gateway's in-memory `_sandboxes` dict; the JSONL sink and its PVC.

**Done when:** a run record and its sandbox registration survive a gateway
restart mid-run; the audit story for a run is queryable from Postgres by
`run_id`; nothing writes to `runs/` anymore.

### Step 9 — `packages/compiler`

**Depends on:** step 7; step 8 only for the registry-snapshot input (stub it
to start early).

**Build:**
- The pure function `(blueprint, role_catalog, registry_snapshot) →
  deployment_plan` (§4.1). No cluster side effects, no secrets in agent
  specs, no OASF/Directory dependency.
- Compile-time validations: every workflow node maps to a **hired standing
  role** (never an ephemeral member role); every tool scope names a
  registered gateway backend; every `may_spawn.crew` references a known crew
  template; every crew member has an explicit allow-list; **no
  child-⊆-parent check** (§4.5); budgets cover enabled workflows. Violations
  fail at compile, not at first tool call. The stricter *spawner-only*
  refinement (workflow nodes bind to standing conductors, no standing
  executor counts) becomes a compile error at step 19 (sequencing choice 7)
  — the bridge blueprint must compile until then.
- Emissions, all from the one source of truth:
  - `Company` / `AgentInstance` CRs (standing roster only — for the bridge
    `hello-saas`, the PM and the transitional engineer; exactly one PM
    instance after step 19);
  - per-role and per-crew-member Rego bundles targeting the §5.1 decision
    input, generated from templates in `policies/`;
  - the **infrastructure policy IR** (§5.4) and its projections: namespace
    NetworkPolicy, Job spec constraints (`SandboxPolicy` values), egress
    proxy allowlist, and — evaluation-track only — OpenShell YAML;
  - crew-template artifacts, budget registrations, workflow gate config.
- Preview: diffable plan against a registry snapshot + projected cost, via
  `poseidon blueprint compile|preview` CLI (apply is step 10's concern —
  the compiler never mutates the cluster).
- `policies/` becomes Rego **templates** + template tests. `poc.rego` stays
  alive until step 12.

**Done when:** compiling `hello-saas.yaml` yields a complete plan covered by
golden-file tests; preview shows a minimal diff for a small blueprint edit;
the compiler provably performs no I/O beyond its inputs.

### Step 10 — CRDs + `packages/operator`

**Depends on:** step 9 (consumes its CRs); step 8 (writes instance status).

**Build:**
- `crds/`: `Company` and `AgentInstance` custom resource definitions.
- `packages/operator` (kopf-based) in the platform namespace, reconciling:
  namespace-per-company; deny-all NetworkPolicy with egress only to the
  gateways; standing agent Deployments from `AgentInstance` specs; compiled
  policy-bundle installation per the frozen §2b contract (bundles persisted
  and served over the standard OPA bundle API; OPA polls — no runtime data
  pushes); teardown and revocation on delete. Writes instance status to the
  registry.
- Applying the bridge `hello-saas` yields **two** standing instances: the
  transitional `backend-engineer` (the PoC brain image — implement keeps its
  standing-executor shape until step 19) and the PM (same `packages/agent`
  host, inert until step 17 gives it a toolset and prompt — an instance the
  operator reconciles but nothing dispatches yet).
- Hard boundaries (§3 ownership matrix): the operator never creates ephemeral
  agents and never runs processes.
- `poseidon blueprint apply` = compile + submit CRs.
- The hardcoded `hello-saas` namespace from step 1 is replaced by
  operator-created namespaces; the agent Deployment moves from static Helm
  values to `AgentInstance` reconciliation.

**Done when:** apply of `hello-saas` converges a live company on kind; a
blueprint edit (e.g. change the PM's model tier or a tool scope) converges
with no manual steps; deleting the company removes the namespace and revokes
policy, verified by a failed post-teardown gateway call.

### Step 11 — Workload identity: JWTs verified at the gateway

**Depends on:** step 1. Parallel with steps 9–10.

**Build:**
- Short-lived, audience-scoped Kubernetes service-account projected tokens
  for every standing agent and for the orchestrator worker.
- Gateway-side verification (TokenReview or JWKS) mapping claims →
  `Identity{company, agent_id, lifecycle, role}` — replacing the
  one-function static map, which is exactly the seam the PoC left (§4.9).
  The `platform` lifecycle covers the orchestrator's privileged identity.
- Implement the platform-side half of the frozen ephemeral-identity contract
  (§2b, overview §4.9): the platform signing key and the gateway
  verification path for platform-signed short-TTL JWTs. The mechanism is no
  longer "design here, decide later" — it is settled; step 16a builds the
  fulfill-side mint/revoke on top of this. Revocation before natural expiry
  is a registry-row termination (kill-worker exit criterion).
- Cutover and deletion of the static tokens happen with step 12, together
  with the policy cutover, per §9 ("compiled bundles and JWTs replace
  `poc.rego` and static tokens" as one milestone exit).

**Done when:** all callers authenticate with projected tokens on kind; a
token from the wrong service account or namespace is rejected and audited.

### Step 12 — Compiled policy live: decision input v2, governance regression, cutover

**Depends on:** steps 9, 10, 11.

**Build:**
- Gateway decision input extended to the §5.1 shape: `company`, `lifecycle`,
  `parent_agent_id`, `role`, `run_id`, `workflow`, `tool`, `args`. Parentage
  fields mandatory for ephemeral callers (enforced even though ephemeral
  callers only appear at step 16).
- **Run-binding enrichment** (§5.1, the PoC `data.runs.assignments`
  successor): the gateway resolves every caller against the registry —
  standing → `agent_instances`, ephemeral → `run_participants`, sandbox
  tools → `sandbox_registrations` — and injects the verified binding into
  the decision input. Wrong, stale, or revoked `run_id` claims fail closed.
  No live OPA data pushes (§2b contract).
- Operator-installed compiled bundles become the only served policy. No
  second policy engine, no "declarative mode" special case (§4.7).
- Infra IR wiring completes: `provision_sandbox` consumes the
  compiler-emitted `SandboxPolicy`; the egress proxy allowlist comes from the
  IR projection instead of static config (closes sequencing choice 2).
- Governance regression suite against compiled policy: E2-class injection
  deny + recovery, path-ownership denies (blueprint `policies` →
  enforceable because bundle-apply already supplies changed paths), per-role
  scoping, run-binding checks (wrong/stale `run_id` denied). The E2-class
  tests are exercised **through the transitional standing engineer**
  (sequencing choice 6) — the only principal with `git.open_pr` before crews
  exist; the suite is re-run against the crew executor at step 19.

**Delete:** `policies/poc.rego`, `policies/poc_test.rego`, the
`identity.py` token map, `AGENT_BEARER_TOKEN` / `RUNNER_BEARER_TOKEN`
settings and their Helm plumbing (resolution row 14).

**Done when:** the governance suite is green against policy compiled from
`hello-saas.yaml`; static tokens and `poc.rego` are gone from the tree.

---

## 5. Phase 3 — M3: the multi-role SDLC

Goal: intake → implement → review end-to-end through PM-requested ephemeral
crews as the only execution paths, with a blueprint-configured human merge
gate, metered budgets, and one consolidated audit story.

### Step 13 — Audit schema v2 + multi-source ingestion

**Depends on:** step 8.

**Build:**
- `AuditEvent` v2 in `packages/common` (§5.2): v1 fields plus `company`,
  `lifecycle`, `parent_agent_id`, `workflow`, and
  `source ∈ {gateway, llm-gateway, sandbox-infra, process-gate}`. Args remain
  digests — audit must never become a leak channel.
- One ingestion contract into the registry `audit_events` table. The MCP
  gateway emits v2 immediately; the egress proxy's denies (step 5 plumbing)
  are upgraded onto the contract; process-gate and budget sources are emitted
  by steps 18–20 and 14 respectively.
- One query answers "the whole story for `run_id` X" across all sources —
  this is the E4 promise at MVP scale (§4.10).

**Done when:** a seeded run shows gateway, sandbox-infra, process-gate, and
llm-gateway events in a single `run_id`-correlated query.

### Step 14 — `packages/gateway-llm`: LLM gateway with metering and budgets

**Depends on:** steps 8 (ledger), 11 (identity). Parallel with step 13.

**Build:**
- The stock LiteLLM proxy grows into an owned service (LiteLLM embedded):
  provider keys still live only here.
- **Caller identity:** verify the same workload JWTs as the MCP gateway;
  every call must carry run context (`run_id`, standing parent, ephemeral
  participant, role). Anonymous passthrough is removed.
- **Metering:** every request written to `budget_ledger` under
  `{company, run_id, standing parent, ephemeral participant, role}` — one
  ledger with dimensions, so crew fan-out stays reconcilable.
- **Model-tier routing:** blueprint `model_tier` per role resolves here, fed
  from the compiled plan.
- **Hard-stops per the frozen §2b contract:** exhausting a per-run cap
  blocks further calls locally at the LLM gateway and writes ledger + audit
  events. **No Temporal client in the LLM gateway** — the workflow observes
  the stop (distinguishable budget error surfaced by the dispatched brain;
  ledger headroom checks at activity boundaries, wired fully at step 19) and
  pauses + escalates into an API-visible pending state. Never silent
  degradation. Budget events land in the audit store (step 13 contract).
- Agent-side change: the brain authenticates to the LLM gateway and forwards
  run context.

**Done when:** an unattributed model call is rejected; a run with a tiny cap
hard-stops mid-way, the workflow is visibly paused-escalated, the ledger
fully attributes the spend, and budget events appear in the audit story
(§4.8 exit criteria).

### Step 15 — Gateway tool surface growth

**Depends on:** step 12 (new tools need compiled policy coverage; the Rego
templates from step 9 are extended here).

**Build (per the §4.7 table):**
- `issues.create / read / update / comment` — new backend against **GitHub
  Issues** (§4.7 [adopted]): same external host, same platform `GIT_TOKEN`.
- `git.read` (PR metadata, diffs) and `git.comment` (review findings) — new
  methods on the git backend.
- `git.merge_pr` — new; callable **only by the orchestrator's platform
  identity** (O-1 recommendation A: workflow state gates the call; OPA rule
  restricts the caller; gate outcomes become audit events at step 20).
- `crew.request` — new: validates and **records** a crew request naming
  `{conductor, crew_template, run_id, reason}` (registry `crew_requests`,
  idempotent, expiring). One spawn model (§4.4 rule 3): OPA evaluates
  `may_spawn` against the **conductor** identity whether the request comes
  from the PM calling this tool or from a workflow recording it under the
  PM's authority — the tool and the workflow path write the same row and
  face the same policy. An allowed request is authorization + intent, never
  fulfillment; fulfillment is step 16.
- Rego templates + compiler emissions extended so per-role bundles cover the
  new tools (PM: `issues.*`, `crew.request`; reviewer: `git.read`,
  `git.comment`, `issues.comment`, `sandbox.*`; orchestrator:
  `git.merge_pr`, `sandbox.run_tests`, admin surface).

**Done when:** each new tool is callable by an authorized role and denied
(audited) for every other role; `git.merge_pr` is provably restricted to the
platform lifecycle.

### Step 16 — Ephemeral crews: fulfillment machinery (four contract-first slices)

Previously one step — the densest and least engineered in the plan relative
to its criticality: it named the right outcomes and skipped the hard design
(who creates ephemeral pods, how run scoping reaches OPA, how gateways
authenticate ephemeral callers, what the planner runtime even is). Split so
that 16a–16c each land an independently testable contract and 16d is the
only slice that orchestrates. **Do not start 16d while any of the three
contracts is unlanded.**

**Step 16a — ephemeral identity mint/revoke.** *Depends on: 11, 8.*
Implement the fulfill-side half of the frozen §2b identity contract: a mint
activity issuing platform-signed short-TTL JWTs (claims = the §5.1 fields);
gateway verification = signature (step 11) + live `run_participants` row;
revocation = row termination; mint and revoke emit `process-lifecycle` audit
events.
*Done when:* a minted identity calls the gateway successfully; the same call
after row termination is denied **before** TTL expiry.

**Step 16b — run-binding install and teardown.** *Depends on: 8, 12.*
`run_participants` lifecycle: created at fulfill with full parentage,
terminated on every terminal path. Nothing here compiles or installs policy
logic — the per-crew-member bundles were compiled at apply (step 9) and
served since step 10; what fulfill installs is **binding data only**
(overview §4.4). The step-12 gateway enrichment picks the rows up
automatically.
*Done when:* a correctly bound ephemeral caller is allowed; a stale or
foreign `run_id` claim is denied; OPA demonstrably receives the binding as
input, never as pushed data.

**Step 16c — ephemeral runtime provisioner.** *Depends on: 4.* The
orchestrator worker gains a bounded Kubernetes client for crew pods — a new
trust and RBAC surface that must be designed, not improvised (overview §3
TCB): create/delete **pods only**, in **company namespaces only**, restricted
by `run_id` label selectors; never Deployments or CRs (standing structure
stays operator-owned); the worker's Role manifest is a review artifact.
Planner runtime: none — the planner is LLM-only per the overview §4.4
planner contract and needs no pod beyond the brain host used for dispatch;
the executor gets one crew pod; both share the run's single sandbox through
the gateway. Provisioner actions emit `process-lifecycle` audit events.
*Done when:* a crew pod round-trips on kind; RBAC provably denies pod
creation in the platform namespace and deletion of anything unlabeled.

**Step 16d — fulfill orchestration.** *Depends on: 16a, 16b, 16c, 15, 3.*
- OPA policy for `crew.request`: `may_spawn` against the conductor identity,
  concurrency caps, budget headroom (§4.4 rule 3, §4.5 rule 5 — **no**
  ⊆-parent check). Spawn denials are first-class audited governance events
  (overview §4.5 threat model).
- Fulfill activities: consume a recorded request (PM-issued or
  workflow-recorded under the PM's authority — same row, same policy); mint
  (16a) → bind (16b) → provision (16c) → dispatch the planner, hand its task
  brief to the executor dispatch (the orchestrator-fixed graph is exactly
  that handoff; no free-form crew chat, §4.4 rule 6); revoke identities and
  finalize participant rows on every terminal path — extending step 3's
  compensation so kill-worker leaks nothing.
- Every ephemeral gateway call carries
  `{company, parent_agent_id, agent_id, role, run_id, lifecycle: ephemeral}`
  (§5.1); `role` carries the catalog template (what path-ownership rules
  name), `parent_agent_id` the standing PM.
- Failure rows from §6.4: requests idempotent and expiring (no orphan
  identities if fulfillment never runs); standing parent drained mid-crew →
  abort crew (blueprint drain default).

*Done when:* the §4.4 exit criteria pass — participants appear with correct
parentage on every gateway decision; identities revoked and rows terminal
after teardown *including* worker kill; an unauthorized or over-cap crew
request is denied by rule and audited; a member calling outside its compiled
allow-list is denied at the gateway.

### Step 17 — PM standing agent runtime

**Depends on:** steps 10 (operator deploys standing instances), 15
(`issues.*`, `crew.request`).

**Build:**
- A role-template-driven brain host for the PM — the instance itself has
  existed (inert) since step 10; this step gives it a toolset and prompt.
  Until Phase 4 lands, reuse the existing `packages/agent` host (FastAPI
  dispatch + LangGraph loop): scope a goal, write/read tickets via
  `issues.*`, request crews via `crew.request`. No executor tools — the
  compiled PM bundle must not include `sandbox.*` write paths or
  `git.open_pr` (§4.5 exit: "the PM cannot call executor-only tools", a
  gateway deny, not prompt hope).
- The PM has **no private memory store** (overview §4.5): its cross-run
  state is the ticket backlog plus the registry `runs` history, both
  re-readable through governed tools.
- Dispatched by workflows over agent HTTP like any brain; holds no
  credentials beyond its projected identity token.

**Done when:** the operator deploys the PM from `hello-saas`; the PM responds
to a dispatch and files a ticket; a PM attempt at `sandbox.exec` or
`git.open_pr` is denied at the gateway and audited.

### Step 18 — `intake` workflow

**Depends on:** steps 17, 14 (budget reservation), 15 (`issues.*`).

**Build (§6.1):**
- `workflows/intake.py`: goal text in via CLI/API (`poseidon run intake …`) →
  `create_run` + budget reservation → dispatch the PM to scope → PM writes
  tickets through `issues.*` → **PM issues `crew.request` for
  `feature-delivery` against the scoped ticket** (the intake → implement
  handoff, §6.1 — the recorded, expiring request that a subsequent implement
  run consumes) → run record links ticket URLs.
- Intake is the injection surface for the standing conductor (goal text is
  untrusted input handed straight to the PM) — which is why the
  injected-conductor red-team test (step 23) enters here.
- No human gate by default; the gate slot exists and reads blueprint config
  like any other gate.

**Done when:** a goal becomes GitHub issues plus a recorded `crew_requests`
row under the PM's authority; the `runs` row links the tickets; the PM's
tool calls appear in the audit story under the run's `run_id`.

### Step 19 — `implement` workflow reshaped: crew fulfillment + platform verify gate + the firing diff

**Depends on:** steps 16, 3 — and deliberately **not** step 17 or 18: the
workflow consumes a recorded crew request when one exists (PM-issued at
intake) and records its own under the PM's conductor authority when started
directly (§4.4 rule 3). This resolves the earlier ambiguity where two spawn
models could be read into the same step. The executor brain is the PoC
LangGraph brain until step 22 swaps it (parity rule, §4.3).

**Build (the §6.2 activity table):**
- `create_run` — registry row + budget check.
- `fetch_workspace` — unchanged (`repo.archive`).
- `provision_sandbox` — compiled `SandboxPolicy` + registry-backed
  registration (already true after steps 8/12).
- `fulfill_crew` — **new**: consume or record the crew request for
  `feature-delivery`, then fulfill (step 16d: mint → bind → provision →
  planner → task brief → executor dispatch).
- `dispatch_work` — dispatch into the ephemeral executor's brain with the
  planner's task brief; wait for its **completion claim** — the brain never
  self-delivers (sequencing choice 8).
- `verify` — **new** (sequencing choice 4): platform-run `sandbox.run_tests`
  via the gateway under the orchestrator identity after the brain claims
  completion; red results feed back for a blueprint-bounded number of
  re-dispatches (§4.3 [adopted]). The PoC brain's internal verify is demoted
  to advisory **here**, not at step 22 — no double gate of record.
- `request_delivery` — workflow-directed dispatch, only after `verify` is
  green: the executor commits on `feature/*` → bundle → `git.open_pr`. The
  PoC graph's deliver node is gated behind this dispatch (sequencing choice
  8) so the brain cannot deliver before the gate of record runs.
- `record_artifacts` — PR URL into the registry.
- `teardown` — sandbox + ephemeral identities + participants, on every
  terminal path.
- **The firing diff (sequencing choice 6):** with the crew path green, land
  the strict lean-roster compile error (spawner-only workflow bindings, no
  standing executor counts — sequencing choice 7), apply the blueprint diff
  removing the transitional standing engineer, and observe the drain default
  handle any in-flight run. From here the crew is the **only** implement
  path (§4.5; §6.4: a workflow referencing a standing engineer identity
  fails closed). Re-run the step-12 E2-class governance tests through the
  crew executor.

**Delete:** the transitional standing `backend-engineer` entry in
`hello-saas.yaml` and any policy or workflow references to a standing
executor identity.

**Done when:** implement runs with the crew as the only execution path,
whether started from an intake handoff or directly; the PR is opened by the
ephemeral executor with parentage on every decision; delivery provably
cannot happen before platform verify passes; the verify red-path loops and
is bounded; teardown is clean under worker kill; `hello-saas` converges to
exactly one standing `AgentInstance` after the firing diff.

### Step 20 — `review` workflow + HITL merge gate

**Depends on:** steps 16, 15, 13; decisions O-1 (recommendation: A) and O-4
(recommendation: full sandbox from the PR ref) confirmed before or at the
start of this step.

**Build (§6.3):**
- `workflows/review.py`: PR in → the workflow records a crew request for
  `feature-review` under the PM's conductor authority (§4.4 rule 3 — same
  `crew_requests` row and same `may_spawn` policy as an agent-issued
  request; process-initiated is a first-class path, not a bypass) → fulfill
  (step 16d) → review sandbox provisioned **from the PR ref** (reuses the
  implement provisioning verbatim) → the ephemeral reviewer (`sdet`
  template) reads the diff via `git.read`, executes tests in its sandbox,
  posts findings via `git.comment` / `issues.comment`.
- **Merge gate:** the workflow parks on the signal named by blueprint
  `workflows.review.gates.merge` — gate config comes from the compiled plan,
  not hardcoded, which is what §9 requires before the intake→review exit can
  be claimed. Approval arrives as an API signal (`poseidon signal …`); timers
  escalate per blueprint when nobody responds (§6.4).
- Approve → the orchestrator calls `git.merge_pr` under its platform
  identity (O-1 = A); reject → findings loop or close per blueprint config.
- Gate outcomes (approve / reject / timeout) written to the audit store as
  `process-gate` events carrying the workflow id that maps back to `run_id`.
- Review is orchestrator-sequenced over HTTP; no SLIM anywhere on the
  critical path (§6.3 [adopted]).

**Done when:** the full chain runs — intake tickets, implement PR, reviewer
findings posted, gate parked, API approval, gateway merge — every step
correlated by `run_id` in one audit story (MVP exit criterion 3); timer
escalation observable when no one approves.

---

## 6. Phase 4 — M4: the flexible brain (overlaps Phase 3)

Goal: a much more capable coding brain behind unchanged governance. Hard
rule: brain adoption and a new sandbox runtime never land in the same slice
(§4.3) — OpenShell stays a non-gating evaluation.

### Step 21 — Brain framework spike (decides O-2, O-3)

**Depends on:** nothing hard; realistic after step 6 (a live cluster to
spike against). Can start any time Phase 3 is underway.

**Build:**
- A time-boxed spike on the OpenHands Agent SDK (plus one peer for
  comparison), against the O-2 criteria: tools cleanly resolvable to gateway
  MCP; no mandatory ownership of workspace/session lifecycle; context
  machinery (condensers, skills) usable headless; license and pace of change.
- Decide O-3 in the same spike: map the brain onto the existing six
  `sandbox.*` tools, or grow the toolserver richer primitives (patch-apply,
  search) for fidelity.

**Done when:** O-2 and O-3 are recorded as decided, with an adapter sketch.

### Step 22 — Hands adapter + brain adoption; delete the PoC graph

**Depends on:** step 21; integrates as the `feature-delivery` executor from
step 19 (or, before step 19 lands, as the direct implement brain).

**Build:**
- Rebuild `packages/agent` as a brain host: the chosen framework as a
  library behind Poseidon contracts (§4.3). The Hands adapter maps the
  brain's tools to gateway MCP calls — `run_id` injection and bundle
  composition carry over from the PoC toolbox.
- Enforced boundaries: the brain never starts/stops sandboxes (orchestrator
  does); never owns process-level retries/timeouts/cancellation (the
  workflow does — the activity dispatches into the brain and waits); its
  internal sub-agents acquire no gateway identity outside the crew mechanism;
  delivery remains bundle → policy → platform push.
- Brain-internal verification becomes advisory; the platform verify gate
  (step 19) is the gate of record.
- If O-3 chose richer primitives: extend the toolserver, gateway routes, and
  Rego templates accordingly.
- Parity: the new brain passes the same happy-path and injection tests as
  the PoC brain on a non-trivial task.

**Delete:** the LangGraph graph and its dependencies, once parity passes —
no dual maintenance (§4.3).

**Done when:** parity suite green with the new brain as the crew executor; a
framework-initiated action outside its tool scope is denied at the gateway
and audited; the PoC graph is gone.

### Step 23 — Red-team suite against the live sandbox backend

**Depends on:** step 6 (live `k8s_jobs` backend); the injected-conductor
test additionally needs steps 17–19 (PM, intake, crew path).

**Build:**
- Scripted suite against live infrastructure, not only OPA denies (§4.6 exit
  criteria): egress exfiltration (through and around the proxy), secret
  scraping (pod env, mounts, cluster metadata endpoints), breakout and
  resource-exhaustion attempts, deadline enforcement.
- **The injected-conductor test** (overview §4.5 threat model, §10 criterion
  4) — E2's analogue for the lean roster, requires steps 17–19: a poisoned
  intake goal steering the PM toward spawning a crew whose work attempts an
  exfiltrating delivery. Asserted containment: the PM cannot mint scopes
  outside the closed crew-template list; the member's compiled allow-list,
  the egress proxy, or the merge gate stops the payload; the spawn attempt
  and every deny are reconstructable from the audit store.
- Every blocked attempt asserted visible in the audit store where a source
  exists for it (proxy denies; NetworkPolicy drops remain the silent
  backstop).

**Done when:** the suite, including the injected-conductor test, runs green
in CI against kind.

### Step 24 — OpenShell evaluation track (strictly non-gating)

**Depends on:** steps 4 (contract), 23 (reuses the red-team suite).

**Build:**
- `packages/sandbox/backends/openshell/` behind the v2 contract; pinned
  version; **zero credential providers**; inference routing disabled or
  pointed at the LLM gateway; its Gateway never reads blueprints or the
  registry.
- OCSF audit export → ingest adapter into the step-13 audit contract (never
  a parallel silo).
- Run the red-team suite against it; write down the O-5 V1 gate criteria
  (multi-tenant posture, API stability under Helm pinning, kind→prod driver
  viability, clean subordinate mode, red-team results, OCSF ingest, honest
  cost comparison vs. hardening `k8s_jobs` ourselves).

**Done when:** an evaluation report with O-5 criteria exists; nothing on the
MVP critical path references OpenShell.

### Step 25 — MVP acceptance: the §10 exit criteria, scripted, on two clusters

**Depends on:** steps 12, 14, 16, 20, 22 (i.e. everything gating).

**Build:**
- One scripted suite covering §10: declarative lifecycle (compile → preview
  → apply → edit → teardown with revocation proof), durability (kill-worker),
  end-to-end SDLC, governance re-proof on compiled policy, budget hard-stop
  + attribution, standing/ephemeral + lean-roster assertions (exactly one
  standing `AgentInstance`; PM/executor scope denials; participant revocation
  under worker death).
- Run identically on a local kind cluster and a real cluster (criterion 7);
  wire into CI where practical.

**Done when:** green on both clusters.

---

## Anytime track (non-blocking)

- **OTel adoption** through the existing `common/telemetry.py` seam (§1
  seam table). Best done during Phase 1 while the deployment story is being
  rebuilt; blocks nothing.
- **Doc rewrite** of the building-block documents per §14 — follows the code,
  never gates it.
- `pyproject.toml` workspace updates ride along with each package step (new
  members: `schema`, `compiler`, `operator`, `registry`, `gateway-llm`;
  rename `runner` → `orchestrator`).

---

## 7. Dependency graph

```text
Phase 1:  1 (= 1a►1b►1c►1d) ──► 2 ──► 3 ──► 6
          1 ──► 4 ──────────────────────────► 6
          1 ──► 5 ──────────────────────────► 6

Phase 2:  7 ──────────► 9 ──► 10 ──► 12
          3 ──► 8 ─────► 9            12 ◄── 11 ◄── 1
          (7, 8, 11 parallel; 12 is the cutover; 10 applies the
           bridge roster — PM + transitional engineer)

Phase 3:  8 ──► 13            11, 8 ──► 14
          12 ─► 15 ─► 16 (16a,16b,16c ─► 16d) ─► 19
          10, 15 ─► 17 ─► 18   (17/18 feed 19's intake handoff
                                but do not gate it — 19 records
                                its own crew request when started
                                directly)
          16, 15, 13 ─► 20
          (13, 14 parallel with 15–17; 19 ends with the firing
           diff that removes the transitional engineer)

Phase 4:  21 ─► 22 (parity vs 19's executor path)
          6 ──► 23 ─► 24   (23's injected-conductor test also
                            needs 17–19)
          all ─► 25
```

Longest path: 1 → 2 → 3 → 8 → 9 → 10 → 12 → 15 → 16a–d → 19 → 20 → 25.
Steps 4, 5, 7, 11, 13, 14, 17, 21, 23, 24 and the anytime track hang off it
in parallel. Implement never goes dark along this path: the PoC standing
executor carries it through step 10, the transitional engineer from 10 to
19, and the crew from 19 on.

---

## 8. Step ↔ MVP exit criteria (§10)

| §10 criterion | Proven by steps |
| --- | --- |
| 1. Declarative lifecycle | 7, 9, 10, 12, 19 (the firing diff is the live hire/fire demo; scripted at 25) |
| 2. Durable / kill-worker | 3, 16a–16d (crew cleanup + revocation-before-TTL), 25 |
| 3. End-to-end SDLC | 15–20, 25 |
| 4. Governance re-proof | 6 (M1 equivalents), 12 (compiled, via the transitional engineer), 19 (re-run via the crew executor), 5+23 (egress/audit), 23 (injected-conductor), 25 |
| 5. Budgets | 14, 25 |
| 6. Standing/ephemeral + lean roster | 9 (compile checks), 10, 16a–16d, 17, 19 (strict roster error + firing diff), 25 |
| 7. kind/real parity | 1a (structure), 25 (proof) |

---

## 9. Deletion schedule (no backwards compatibility)

| Deleted | At step | Replaced by |
| --- | --- | --- |
| `poseidon-poc` CLI entry point | 3 | `poseidon run\|signal\|status` |
| `deploy/compose/`, Docker sandbox backend | 6 | Helm + kind, `k8s_jobs` |
| `runs/<id>/manifest.json`, gateway in-memory sandbox dict, JSONL audit sink | 8 | Registry tables |
| `policies/poc.rego` + tests, static token map (`identity.py`), bearer-token settings | 12 | Compiled bundles, workload JWTs |
| Anonymous LLM proxy passthrough | 14 | Authenticated, metered LLM gateway |
| Transitional standing `backend-engineer` (blueprint entry + standing-executor references) | 19 | `feature-delivery` crew under PM conductor authority |
| Brain-owned deliver-on-green (PoC graph's self-delivery) | 19 | Workflow-directed `request_delivery` after platform verify |
| LangGraph PoC graph | 22 | Adopted framework behind Hands |

---

## 10. Open decisions and where they block

| Decision | Must be settled | Default carried by this plan |
| --- | --- | --- |
| O-1 — approvals visible to OPA? | before step 20 | A: workflow state gates `git.merge_pr`; caller restricted to platform identity; gate outcomes as first-class audit events; merge approval is thereby the one *named* sole-ownership exception to defense in depth (overview §2) and needs that explicit sign-off; revisit B at V1 |
| O-2 — brain framework | by step 21 (the spike decides it) | OpenHands Agent SDK is the candidate, not the default |
| O-3 — Hands tool-surface width | by step 21, consumed at 22 | — |
| O-4 — review sandbox shape | before step 20 | Full sandbox from the PR ref (reuses implement plumbing; costs a second sandbox per feature) |
| O-5 — OpenShell V1 gate criteria | written down during step 24 | Decision itself is V1, non-gating for MVP |
