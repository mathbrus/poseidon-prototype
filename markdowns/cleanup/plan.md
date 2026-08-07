# Poseidon — post-engineering cleanup plan

**Branch:** `feature/mvp` · **Scope:** the nine points in `markdowns/cleanup/instructions.md`
**How to use this document:** execute the steps in order, one at a time. Each step is
self-contained: goal, actions, verification, and a commit. Suggested session pacing:
(0+1+2) · (3a–3d) · (3e–3g+4) · (5) · (6+7) · (8) · (9+10).

Decisions already made by the user (do not re-ask):

- Delete the non-adopted OpenShell sandbox backend.
- Security: **fix** cheap/safe hardening *and* the two recorded-not-fixed MVP findings
  (missing `securityContext`, DNS tunnelling via CoreDNS); **document** by-design items
  (unauthenticated in-cluster services behind NetworkPolicy, placeholder Helm secrets)
  in the root README's limitations section.
- CI additions: ruff lint + format gate, `uv lock --check`, pip-audit (advisory),
  helm lint/template. The kind e2e job stays manual-dispatch.
- Rename process-named artifacts: `scripts/mvp-acceptance.sh` → `scripts/acceptance.sh`.

---

## Ground rules (apply to every step)

1. **Tests run per-package only**: `uv run pytest packages/<pkg>`. Root-wide collection
   is broken by design (duplicate test module basenames across packages). The registry
   suite (28 tests) skips without `REGISTRY_DSN` — expected locally; it runs for real
   only in CI against Postgres.
2. **Golden files are generated.** `packages/compiler/tests/golden/hello-saas-bundle/*`
   is emitted by `packages/compiler/src/compiler/rego.py`. Never hand-edit goldens.
   After any change to the generator (including emitted comment text):
   `uv run python -m compiler.regolden`, then `uv run pytest packages/compiler`, then
   inspect the golden diff to confirm it matches intent.
3. **Never set `REAL_LLM`.** The kind e2e suite uses the deterministic mock LLM by
   default; a real provider costs money and requires explicit user opt-in.
4. **One commit per step** (sub-steps of step 3 and step 5 commit individually), message
   prefixed `cleanup:`. Any step must be independently revertable.
5. **Never carry red forward.** If a step's verification fails, fix it inside the step.
6. **`deploy/kind/.env` holds live credentials** (GitHub PAT, Anthropic key). It is
   untracked and verified absent from git history. Never print, move, copy, or commit
   its contents.

---

## Step 0 — Preflight: confirm the green baseline (S)

**Goal:** prove the starting state is green so later failures are attributable.

- `git status` — on `feature/mvp`; expected untracked: `deploy/kind/.env`, `runs/`,
  `markdowns/cleanup/`.
- Run all 11 suites:
  `for p in packages/*/; do uv run pytest "$p" -q || break; done`
- `helm lint deploy/helm/poseidon` and `helm template deploy/helm/poseidon > /dev/null`.

**Verify:** all suites pass (registry skips OK), helm clean. No commit (nothing changed).

---

## Step 1 — Remove dead PoC leftovers (S)

**Goal:** delete code that exists only as a bridge from the PoC/evaluation era.

- Delete `packages/sandbox/src/sandbox/backends/openshell/` (evaluated at M4,
  explicitly not adopted; the only code referencing `markdowns/`).
- Delete `packages/sandbox/tests/test_openshell.py`.
- Remove the `openshell` optional-dependency extra from `packages/sandbox/pyproject.toml`,
  then `uv lock`.
- Delete the untracked `runs/` directory (PoC filesystem run-store + JSONL audit,
  superseded by the Postgres registry) and remove the dead `!runs/.gitkeep` negation
  from `.gitignore` (git cannot un-ignore inside an ignored dir; the rule is a no-op).
- Delete the empty `.claude/skills/run-poseidon-prototype/` directory.
- Sweep for stragglers: `grep -rni openshell packages/ deploy/ scripts/ tests/ .github/ Dockerfile.*`
  — remove any hits.

**Do NOT delete:** `packages/registry/src/registry/memory.py` (test doubles imported by
other packages' tests), `packages/compiler/src/compiler/regolden.py` (golden regeneration),
`packages/mockllm` (the e2e default brain).

**Verify:** `uv run pytest packages/sandbox`; `uv lock --check`; the grep returns nothing.
**Commit:** `cleanup: remove OpenShell backend and PoC run artifacts`.

---

## Step 2 — Security audit + triage (M)

**Goal:** a thorough, read-only security pass producing a triage list (kept in the
executor's scratchpad, not committed). Every finding lands in a bucket:

- **A — fix in step 5** (cheap + safe, pre-approved),
- **B — fix in step 5, e2e-gated** (the two recorded MVP findings),
- **C — document in the root README's limitations section (step 9).**

Seeded findings to verify and extend (look for more of each class):

1. **Secrets.** Re-confirm `deploy/kind/.env` is untracked and absent from history
   (`git log --all --diff-filter=A -- deploy/kind/.env`; spot-check history for
   `github_pat_` / `sk-ant-` prefixes). Scan tracked files for credential patterns.
   → Recommend to the user (in the step-10 summary): rotate both credentials as a
   precaution. Bucket C for the recommendation itself.
2. **Pod security.** Zero `securityContext` anywhere; no Dockerfile has `USER`.
   Fix sites covering the whole fleet:
   `packages/sandbox/src/sandbox/backends/k8s_jobs/manifest.py::build_job`,
   `packages/orchestrator/src/orchestrator/crewpods.py`,
   `packages/operator/src/poseidon_operator/resources.py::agent_deployment`,
   each pod spec in `deploy/helm/poseidon/templates/*.yaml`. → **A**.
3. **DNS tunnelling** (recorded finding #2). kube-dns egress is allowed from sandbox
   and agent networks: `packages/operator/src/poseidon_operator/resources.py`
   (agent egress + `_peer_rule`), `deploy/helm/poseidon/templates/networkpolicies.yaml`,
   allowlist in `packages/compiler/src/compiler/infra.py`. Measured (not prevented) by
   `tests/e2e_kind/test_redteam.py`. → **B**: narrow sandbox DNS (drop the kube-dns peer
   where resolution isn't needed, or add resolver-level policy). Gate on the step-7 e2e
   run; if resolution genuinely breaks the suite, fall back to documenting with rationale.
4. **Git operations.** `packages/gateway/src/gateway/git_backend.py`: PAT embedded in
   clone URL with redaction applied only to error text; caller-supplied `repo`/`ref`
   passed positionally with no `--` separator; no URL scheme allowlist (git's `ext::`
   transport executes commands). → **A**: add `--` separators / leading-dash rejection,
   allowlist `https://` (+`ssh://` if used), extend redaction to all log paths.
5. **Prompt assembly.** `packages/agent/src/agent/brain.py`: `str.format` on the system
   prompt template (→ **A**: safe substitution / escape braces); workspace filenames and
   raw test output flow into prompts (→ **C**: inherent to the design, document as part
   of the prompt-injection threat model alongside the containment guarantees).
6. **Duplicated slug logic.** `packages/gateway/src/gateway/opa.py` and
   `packages/compiler/src/compiler/rego.py` independently implement the same slug
   transform; divergence fails closed (denies) but the invariant is untested. → **A**:
   add a cross-package invariant test.
7. **Helm secrets.** `deploy/helm/poseidon/values.yaml` ships literal placeholder values
   including the HS256 platform-JWT signing key; a bare `helm install` deploys them
   silently. → **A**: `required`-value pattern (template fails without an override),
   keeping the kind dev-fallback flow working. Also note the single `llmApiKey` written
   to both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in `templates/secrets.yaml`.
8. **Supply chain.** All image tags `:latest`/moving (incl. `openpolicyagent/opa:latest`,
   `ghcr.io/berriai/litellm:main-stable`); base images unpinned. → **A**: pin third-party
   images to versioned tags in `values.yaml`; CI gains `uv lock --check` + pip-audit in
   step 6. Digest-pinning of base images → **C** (note as policy decision).
9. **By-design surfaces** → **C**, document precisely in step 9: unauthenticated
   sandbox toolserver (`shell=True /exec` — it *is* the sandbox; NetworkPolicy-guarded),
   agent `/task`, bundle server, OPA API; hand-rolled JWT in
   `packages/common/src/common/platformjwt.py` (small, `alg`-pinned; replacing it is
   out of cleanup scope); NetworkPolicies require an enforcing CNI (Calico on kind).
10. **Fresh sweeps** for anything the inventory missed:
    `grep -rn "shell=True\|yaml.load(\|pickle\|verify=False\|os.system\|eval(" packages/`,
    Dockerfile review (root user, secrets in layers), CI log-exposure review,
    RBAC review (the operator ClusterRole holds `escalate`/`bind` — deliberate; confirm
    scoping and document).

**Verify:** triage list complete, every item bucketed with a named fix site;
`git status` shows no repo modifications.
**Commit:** none (read-only step).

---

## Step 3 — Comment and reference sweep (XL — 7 sub-steps)

**Goal:** code describes its current state only. Remove ~285 "plan step N", ~354 "§N",
36 "O-N", 17 "sequencing choice N", 26 "PoC", 11 "M1–M4" refs across 159 files.

**Rewrite rules:**

- *"plan step 12 introduced X to do Y"* → "X does Y." Delete entirely if self-evident.
- *"per §4.2 / O-1 A / sequencing choice 7"* → if the rationale is load-bearing (a
  non-obvious invariant, a fail-closed decision, a deliberate limitation), inline it in
  one or two sentences — resolve the reference against `markdowns/` **now, while it
  still exists**; otherwise drop the reference.
- Milestone/PoC framing → delete; if "PoC" marks a real limitation, restate it plainly
  ("Not hardened: X").
- Agent-thought narration ("Let's now…", "We need to…", stepwise storytelling) →
  terse descriptive comment or deletion.
- **Keep:** security rationale, fail-closed explanations, "why not the obvious
  approach" notes, invariants other code depends on.
- **No behavior changes in this step** — comments, docstrings, and description strings
  only. (Exception: `compiler/rego.py` emits comment text into generated Rego —
  follow the regolden rule.)
- Also fix two **factually wrong** docstrings:
  `packages/orchestrator/src/orchestrator/gates/__init__.py` claims no gate is wired
  (the review workflow wires one); `packages/orchestrator/src/orchestrator/__init__.py`
  layout docstring omits the intake/review workflows.
- Find targets per sub-step:
  `grep -rnE 'plan step|§[0-9]|O-[0-9]|sequencing choice|PoC|M[1-4]\b|milestone|overview §' <paths>`
  (expect false positives — judge each hit).

**Sub-steps** (each: edit → run the affected package suites → commit):

- **3a — compiler.** Top offender `rego.py` (16 hits). Regolden + inspect the golden
  diff (comment-only changes expected).
- **3b — common, schema, gateway, gateway-llm.** `gateway/service.py` has 18 hits.
  Fix pyproject descriptions: `common` and `gateway` say "PoC", `gateway-llm` says
  "(plan step 14)".
- **3c — orchestrator.** Workflows, activities, gates; the two factual fixes above.
- **3d — agent, sandbox, mockllm.** `agent/brain.py`, `mockllm/brain.py`,
  `sandbox/contract.py`.
- **3e — registry, operator.** `registry/schema.sql` — comment changes only, zero DDL
  changes; `operator/resources.py`.
- **3f — deploy, Dockerfiles, crds, examples.** All 8 Dockerfiles, Helm templates,
  `Chart.yaml` ("Poseidon M1" description), `deploy/kind/*.sh`, `crds/*.yaml`,
  `examples/hello-saas.yaml`. Verify with `helm lint` + `helm template`.
- **3g — tests, scripts, CI file.** `tests/e2e_kind/*` docstrings, `.github/workflows/ci.yml`
  comments, and **rename `scripts/mvp-acceptance.sh` → `scripts/acceptance.sh`**
  (update every reference: `ci.yml`, comments, any docs; keep the §10-table output
  wording state-descriptive). Verify: `uv run pytest tests/e2e_kind --collect-only -q`
  and `bash -n scripts/*.sh`.

**Final check:** the repo-wide grep returns only legitimate hits. `README.md` is exempt
(rewritten in step 9); `markdowns/` is exempt (deleted next).

---

## Step 4 — Delete the historical `markdowns/` content (S)

**Goal:** the folder is historical; content survives in git history.

- `git rm -r markdowns/MVP markdowns/poc`.
- **Keep `markdowns/cleanup/`** (this plan + instructions) until step 10.
- `grep -rn "markdowns/" --include="*" .` (tracked files) — zero hits expected outside
  `README.md`; note the README hits for step 9.

**Verify:** grep as above; smoke-test `uv run pytest packages/compiler packages/sandbox`.
**Commit:** `cleanup: delete historical design docs (markdowns/MVP, markdowns/poc)`.

---

## Step 5 — Simplification + security fixes (L)

**Goal:** apply the step-2 bucket A and B fixes, then a general simplification pass —
now that the system is complete, remove what no longer makes sense.

**Security fixes (small individual commits):**

- `securityContext` at the four fleet-covering sites + Helm templates (runAsNonRoot,
  `allowPrivilegeEscalation: false`, drop all capabilities, seccomp `RuntimeDefault`,
  `readOnlyRootFilesystem` where the workload tolerates it) + `USER` in Dockerfiles.
  Watch for workloads that legitimately need writable paths (git worktrees in the
  sandbox) — mount `emptyDir` rather than weakening the context.
- DNS-tunnelling narrowing (bucket B) — e2e-gated per step 2 item 3; the redteam DNS
  test may need updating from "measures" to "asserts blocked" if the fix lands.
- `git_backend.py` hardening: `--` separators, scheme allowlist, full-path redaction.
- `brain.py` prompt-template hardening.
- Cross-package slug-invariant test (place where both implementations are importable,
  e.g. a compiler-dev-dependency test in `packages/gateway/tests/`).
- Helm `required` secrets pattern; pin third-party image tags in `values.yaml`.

**General simplification (per package, same order as step 3):** dead branches, unused
imports/params, single-caller indirection left from bridging eras, duplicated helpers,
over-general abstractions with one concrete use, config knobs nothing sets. Bridge-era
test fixtures (e.g. the `bridge` fixture in `packages/compiler/tests/conftest.py` and
schema-test bridge shapes) — keep the behavioral guarantees they pin, rename/reword to
describe the current rule ("standing executor is a compile error") rather than the
migration story. Do **not** restructure package boundaries, rename public modules, or
change wire/CRD/DB schemas.

**Verify:** all 11 package suites; `helm lint` + `helm template`; regolden if
`rego.py` was touched. (The kind e2e run happens once, in step 7.)

---

## Step 6 — CI overhaul (M)

**Goal:** CI that actually fires and passes. Diagnosis: the workflow triggers on
`push: [main]` but the default branch is `master` (the trigger has never fired), and
`master` still carries the incompatible PoC-era workflow (references deleted
`policies/` and `deploy/compose/`) — that is the red CI. The master side resolves via
the step-10 PR; this step fixes the workflow on `feature/mvp`.

Edit `.github/workflows/ci.yml`:

- **Triggers:** `push: branches: [master]`, `pull_request` (all), keep
  `workflow_dispatch` with the `run_e2e` input.
- **New `lint` job:** `uv run ruff check .` + `uv run ruff format --check .`.
  Add `ruff` to the root dev group and a minimal `[tool.ruff]` in the root
  `pyproject.toml`; run `ruff format` once repo-wide here (single mechanical commit,
  after step 5 so formatting doesn't churn twice).
- **New `lock` job:** `uv lock --check`, then advisory dependency audit
  (`uv export --frozen | uv run pip-audit -r -` or equivalent; `continue-on-error: true`).
- **New `helm` job:** `helm lint deploy/helm/poseidon` + `helm template` with default
  and `values-kind.yaml` values.
- **`unit`:** replace the hardcoded 11-package list with dynamic discovery
  (`for p in packages/*/;`); keep the Postgres service + `REGISTRY_DSN` so the registry
  suite runs for real.
- **`opa`:** keep (compile example blueprint + `opa check`) — pin the OPA image tag.
- **`images`:** keep all three parts (builds, mcp lockfile-pin spot-check,
  secret-inspection gate); derive the image list dynamically from `Dockerfile.*` at
  repo root; add `uv sync` or guard the bare `python` usage in the pin check.
- **`e2e-kind`:** keep manual-dispatch-only; confirm nothing sets `REAL_LLM`; update
  the script name to `scripts/acceptance.sh`.

**Verify locally:** `ruff check .` and `ruff format --check .` green,
`uv lock --check` green, `actionlint` if available (else careful YAML review).
**Commits:** one for ruff config + reformat, one for the workflow.

---

## Step 7 — Full kind acceptance run (S effort, ~17 min wall)

**Goal:** one end-to-end validation of every behavior change (steps 1 and 5) on kind.

- `scripts/acceptance.sh` (mock LLM; `REAL_LLM` unset). Requires the kind cluster from
  `deploy/kind/kind-up.sh` and the existing `deploy/kind/.env`.
- If the DNS narrowing from step 5 is in: confirm the redteam DNS expectation matches.

**Verify:** all 90 e2e tests pass. On failure, bisect against the per-step commits and
fix in a follow-up commit attributed to the offending step.
**Commit:** only if fixes were needed.

---

## Step 8 — Per-package READMEs (L)

**Goal:** one `README.md` in each of the 11 `packages/*/` directories, describing
internals for a developer landing there.

Template: purpose (one paragraph) · module map (what each source file owns) · entry
points and invocation (service/CLI/library, which Dockerfile) · cross-package
interactions (imports, wire contracts) · running the tests (per-package command;
registry's `REGISTRY_DSN` caveat; compiler's regolden workflow) · notable invariants.

Package-specific notes to include: `mockllm` is the e2e default brain (cost guardrail);
`registry/memory.py` doubles are intentionally shipped for cross-package tests;
`compiler` goldens are generated; `sandbox` README records the OpenShell evaluation
outcome (posture borrowed, backend not adopted — see git history); `gateway` is the
only holder of `GIT_TOKEN`; `gateway-llm` meters and budget-gates model access;
`operator` owns CRDs + OPA bundle serving; `orchestrator` owns Temporal workflows and
the human merge gate; `agent` hosts the OpenHands SDK loop; `common` owns identity,
audit, and the platform JWT; `schema` owns the blueprint contract.

**Verify:** spot-check that stated commands run; apply the step-3 process-vocabulary
grep to the new files (zero hits).
**Commit:** one, or one per package if sessions split.

---

## Step 9 — Root README rewrite (L)

**Goal:** replace the 880-line PoC-era README (deleted `packages/runner`, Docker
Compose networks, LangGraph, JSONL audit, nonexistent `deploy/compose/`, broken test
commands, 15 dead `markdowns/` links) with a manual that reads like a book for someone
learning the topic.

Structure:

1. **What Poseidon is** — one screen; the thesis (never trust the agent, constrain it:
   enforcement by infrastructure, not prompts) — and explicitly what it is **not** yet.
2. **Concepts** — MCP, OPA/Rego, prompt injection, workload identity, NetworkPolicy,
   Temporal — each in a short note, only what the reader needs.
3. **Architecture tour** — how a company blueprint flows: schema → compiler (plan +
   Rego bundles) → operator (CRDs, namespaces, NetworkPolicies, bundle server) → OPA →
   orchestrator (Temporal workflows, crew pods, gates) → agent (OpenHands SDK) →
   sandbox (k8s Jobs, egress proxy) — with `gateway` and `gateway-llm` as the two
   credential-holding chokepoints and `registry` (Postgres) as the audit/run store;
   `mockllm` for development.
4. **Getting started** — prerequisites; uv workspace setup; per-package tests (and why
   root-wide collection fails); kind bring-up (`deploy/kind/kind-up.sh`, `.env.example`);
   the acceptance suite (`scripts/acceptance.sh`, mock LLM by default, what `REAL_LLM`
   opt-in means and costs).
5. **Operating guide** — Helm values, the two CRDs, a walkthrough of
   `examples/hello-saas.yaml` and the example tasks (happy, injected, verify-red).
6. **What it can and cannot do** — capabilities honestly stated; then **limitations &
   threat model** from step 2 bucket C (unauthenticated in-cluster surfaces and their
   NetworkPolicy guards, hand-rolled JWT, image/tag policy, placeholder dev secrets,
   prompt-injection posture, anything documented-not-fixed).
7. **Development guide** — regolden workflow, registry DSN, CI overview, repository map.
8. **Links** to the 11 package READMEs.

**Verify:** every command in the README executes; every relative link resolves; zero
`markdowns/` references remain repo-wide; the process-vocabulary grep is clean.
**Commit:** `cleanup: rewrite README for the current system`.

---

## Step 10 — Final sweep and push to remote (S)

- Repo-wide grep: `PoC|plan step|§[0-9]|O-[0-9]|M[1-4]\b|milestone|sequencing choice|markdowns/`
  — judge every residual hit.
- All 11 package suites; `helm lint`.
- `git log --oneline` review — coherent, per-step commits.
- Delete `markdowns/cleanup/` (this plan + instructions — both preserved in git
  history), removing the now-empty `markdowns/` folder entirely.
- Push `feature/mvp`; If possible: (`gh` is not installed — install it or
  ask the user to run `! gh auth login`; title it as the MVP + cleanup delivery). The
  PR's CI run proves the new workflow; merging replaces master's broken PoC workflow.
- Write a closing summary for the user: what was fixed, what was documented-not-fixed
  (bucket C), and recommended follow-ups — **rotate the GitHub PAT and Anthropic key
  in `deploy/kind/.env`**, review the PR, and any bucket-C items worth a future
  effort (JWT library, digest pinning, CoreDNS-level DNS policy if not fixed).

---

## Sizing summary

| Step | Size | Session note |
|---|---|---|
| 0 preflight | S | |
| 1 dead code | S | |
| 2 security audit | M | read-only, produces the triage list |
| 3 comment sweep | XL | 7 sub-steps, natural session boundaries |
| 4 markdowns deletion | S | keeps `cleanup/` until step 10 |
| 5 simplify + fixes | L | behavior changes live here |
| 6 CI | M | includes the one-time ruff reformat |
| 7 kind e2e | S (+17 min) | the single full-stack gate |
| 8 package READMEs | L | 11 files, splittable |
| 9 root README | L | |
| 10 sweep + PR | S | ends with the push to remote |
