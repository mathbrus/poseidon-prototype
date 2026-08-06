# Step 24 — OpenShell evaluation: the O-5 gate criteria, judged

Date: 2026-08-06. Scope: plan §6 step 24 — put NVIDIA OpenShell behind the
v2 sandbox contract, adapt its OCSF export into our audit contract, and
write down the O-5 criteria that decide, **at V1**, whether it replaces the
self-operated `k8s_jobs` hardening path. Strictly non-gating: nothing on
the MVP critical path references it, and a test enforces that.

## Verdict

**Do not adopt at V1 on today's evidence; re-evaluate when multi-tenancy
ships and the API leaves `0.0.x`.** Two criteria fail outright
(multi-tenant posture, API stability) and one contract mismatch costs real
work (no per-sandbox address). Against that, OpenShell's isolation
primitives are genuinely better than what `k8s_jobs` has today — which is
an argument for *borrowing its posture*, not for adopting its runtime.

The cheapest correct move is the one this evaluation makes possible:
**take the hardening ideas, keep the boring backend.** OpenShell exposes
`runtime_class_name`, user namespaces, Landlock and a process policy —
exactly the isolation §4.6 defers to V1 and exactly what step 23's
red-team suite found missing from our own sandbox (no `securityContext` at
all). Those are three lines of pod spec on `k8s_jobs`, not a runtime
migration.

## What was actually built and verified

- `packages/sandbox/backends/openshell/` — the v2 contract
  (`provision`/`destroy`) against the real `openshell==0.0.99` client:
  create → wait_ready → workspace delivery over `exec` stdin → labelled
  handle; idempotent destroy. Behind an optional extra so it can never
  ride into the sandbox image.
- `backend.build_spec` — the policy mapping, **verified against the real
  protobuf**, not just against a mock: image, env (the §2b egress
  credential), filesystem scope, CPU/memory/ephemeral-storage.
- `ocsf.to_audit_event` — OCSF v1.7 → the step-13 `AuditEvent` contract,
  `source=sandbox-infra`, args digested, timestamps preserved, unmapped
  classes refused rather than guessed.
- 10 unit tests, plus a structural test asserting the non-gating rule.

Not built, deliberately: a live OpenShell deployment on the kind cluster.
It would have measured an alpha's install ergonomics, not the criteria
that decide O-5 — and the two failing criteria below are settled by
published behaviour, not by a local run.

## The O-5 criteria

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Multi-tenant posture compatible with namespace-per-company | **Fail** | The project describes itself as "alpha software — single-player mode… one developer, one environment, one gateway". Multi-tenancy is roadmap, not a capability. Our platform is namespace-per-company by construction (§4.9), so this is disqualifying today, not a rough edge. |
| 2 | API stability compatible with Helm pinning | **Fail** | Still `0.0.x` after ~1,100 commits, with releases most days (v0.0.92 → v0.0.99 inside ten days; v0.0.99 shipped the day before this evaluation). No semver promise, no deprecation policy, and "Providers v2" already coexists with legacy provider attachment. Pinning works mechanically (`openshell==0.0.99`); pinning to a line that moves daily means owning a permanent upgrade treadmill. |
| 3 | kind→prod driver viability | **Mixed** | A Kubernetes driver exists and follows the SIG Apps agent-sandbox CRD spec — a genuinely durable bet, since that standard may outlive any implementation. But the README calls the Kubernetes path experimental with breaking changes expected, and known limits bite our shape: `volumeClaimTemplates` immutable after creation, pods pending without a default StorageClass. |
| 4 | Clean subordinate mode | **Pass, with work** | Everything §4.6 demands can be switched off: `providers` stays empty (verified — the field is a plain repeated field on `SandboxSpec`), and no inference route need be configured, so model calls stay on the metered LLM gateway (§4.8). Its Gateway is handed an endpoint and a bearer token and nothing else — no blueprint, no registry. **But** one documented capability could not be verified: a global switch to disable credential providers outright. We can decline to *use* them; we could not confirm we can *forbid* them. |
| 5 | Red-team results | **Not run — and the reason matters** | Step 23's suite attacks a sandbox through our toolserver at an HTTP address. OpenShell publishes no per-sandbox address (below), so the suite cannot be pointed at it without first building the shim. What the spec surface shows is nonetheless favourable: Landlock, user namespaces, `runtime_class_name` and a `run_as_user`/`run_as_group` process policy are all first-class — strictly more than `k8s_jobs` enforces today. |
| 6 | OCSF ingest proven | **Pass on shape, fail on trust** | The mapping is real and tested (`ocsf.py`): seven event classes, `disposition_id` → allow/deny, into one `run_id`-correlated story with `source=sandbox-infra`. Two limits are structural, not fixable by an adapter: records are written **inside the sandbox** (`/var/log/openshell-ocsf.*`, 3 files retained, file-drop rather than push), so the audited workload can reach its own audit trail — the opposite of our posture, where every producer writes to Postgres from outside; and OCSF records carry no `run_id`, so attribution is ours to supply from the sandbox label. |
| 7 | Honest cost vs. hardening `k8s_jobs` ourselves | **Favours hardening ourselves** | See below. |

## Criterion 7, in detail: the honest comparison

**Adopting OpenShell** costs: a C4-speaking shim (the contract gap below),
a drain-and-ingest job for OCSF, an upgrade treadmill against a daily
`0.0.x`, and a multi-tenancy story that does not exist yet — while
replacing a backend that currently works and is already proven by 85 e2e
tests.

**Hardening `k8s_jobs`** costs: a `securityContext` block. Step 23 found
our sandbox runs with no `securityContext` at all — no `runAsNonRoot`, no
`allowPrivilegeEscalation: false`, no capability drop, no seccomp profile.
Adding `drop: [ALL]`, `allowPrivilegeEscalation: false` and
`seccompProfile: RuntimeDefault` needs no non-root migration and no new
runtime. A hardened `RuntimeClass` (gVisor/Kata) is a values change on the
same manifest. That is the same isolation OpenShell is attractive *for*,
at a fraction of the integration cost, on infrastructure we already
operate.

The asymmetry is the finding: OpenShell's appeal is its **posture**, and
posture is portable.

## The contract gap that drives most of the cost

Contract C4 requires the run's toolserver to be reachable by the MCP
gateway at an HTTP URL; `SandboxHandle.url` carries it. OpenShell
publishes **no per-sandbox address** — `SandboxRef` is
`{id, name, workspace, status, labels}` and every interaction is mediated
by its Gateway's gRPC `exec`.

So adoption requires a **translating shim**: an HTTP service speaking C4
that turns `sandbox.read_file` / `write_file` / `exec` / `run_tests` /
`git_bundle` into OpenShell `exec` calls. That is not a wrapper detail —
it moves the toolserver *outside* the sandbox, which changes the trust
boundary (our toolserver's `resolve()` confinement currently runs inside
the isolation domain), and re-implements file operations as shell
commands, losing that confinement unless it is rebuilt in the shim.

The backend refuses to paper over this: with no `toolserver_url`
configured, `provision` raises `ContractGap` rather than returning a
plausible dead URL, and a test pins that behaviour.

## Smaller findings, recorded so a future evaluator need not re-derive them

- **No wall-clock deadline.** `SandboxSpec` has no `activeDeadlineSeconds`
  equivalent, so `SandboxPolicy.deadline_s` — the §4.6 runaway backstop,
  and a live assertion in step 23's suite — would have to be enforced by
  the orchestrator instead of the runtime.
- **Workspace delivery is nicer.** `exec` accepts real `stdin` bytes, so
  the tar pipes straight in; `k8s_jobs` base64s through the pods/exec
  websocket because that client cannot half-close stdin.
- **"Workspace" is an overloaded word.** OpenShell's `workspace` is a
  tenancy grouping; ours is the run's git checkout directory. The adapter
  keeps them apart deliberately.
- **Egress could be doubled up.** `SandboxPolicy.network_policies` /
  `network_middlewares` could enforce the blueprint allowlist inside the
  runtime as well as at our proxy. Attractive as defense in depth — but
  our proxy is what makes denies *auditable* (§4.6), so it would be an
  addition, never a replacement.

## What would change the verdict

Re-open O-5 when: multi-tenancy ships and maps onto namespace-per-company;
the API reaches a stability commitment (1.x with a deprecation policy);
and the Kubernetes driver loses its experimental label. At that point the
work already done here — the backend, the spec mapping, the OCSF adapter —
is the starting point, and only the shim remains genuinely open.

Until then: `k8s_jobs` proceeds, and the recommended follow-up is the
`securityContext` hardening step 23 identified, which is independent of
this decision.

Sources: github.com/NVIDIA/OpenShell (Apache-2.0) · docs.nvidia.com/openshell
(sandbox-compute-drivers, inference-routing, manage-providers,
ocsf-json-export, kubernetes/setup) · the published `openshell==0.0.99`
package, whose client and protobuf surface were inspected directly for
this report.
