# MVP direction: flexible coding agents under clear governance

## Goal

Future coding agents must solve **arbitrary** software problems — not only the
narrow PoC workflow of “implement a small change, pass tests, open a PR.” The
reasoning loop therefore needs to become much more flexible: open-ended tool
use, strong coding priors, and real context engineering (condensation, skills,
memory) over long horizons.

That flexibility does **not** relax Poseidon’s governance model. Agents still
operate under clear rules: credential isolation, gateway-mediated side effects,
semantic policy, audit, and explicit success criteria (for example, a configured
verify gate such as “repository tests must pass” before delivery). Governance
remains hard and inspectable; only the agent’s *internal* problem-solving
strategy becomes more general.

## What changes

The PoC agent is a phase machine (`plan → act → verify → deliver`) with a small
tool surface and graph-owned verification. That design proves governance, but it
is too rigid for arbitrary tasks:

- a fixed phase graph assumes a known workflow
- a tiny hand-rolled ReAct loop under-invests in coding heuristics and recovery
- ad hoc context clipping does not scale to long-horizon work

For the MVP coding brain, it is reasonable to adopt a mature coding-agent
framework (for example the OpenHands Software Agent SDK, or a peer in that
class). Those systems already provide:

- a robust Action–Observation loop
- typed tools and MCP integration
- conversation condensers / skills / context management
- battle-tested coding tool UX (edit, search, shell, recovery)

In short: **replace the custom reasoning loop where flexibility matters; keep
Poseidon as the enforcement and delivery plane.**

## What does not change

Poseidon’s differentiation remains the control plane, not “best coding agent”:

1. **Agents hold no credentials.** Git tokens and provider keys never live in
   the agent runtime.
2. **No unmediated egress.** Side effects leave only through the MCP gateway
   (and related platform gateways).
3. **Semantic policy + audit.** Branch rules, path rules, budgets, approvals,
   and allow/deny decisions stay gateway/OPA concerns — including clear gates
   such as verify-before-deliver when a company or task requires them.
4. **Sandbox isolation.** Untrusted code execution stays in a credential-free
   workspace with enforced network boundaries.

A coding framework may become the **brain**, but it must not become the system
that owns git push, secrets, or “what is allowed.”

## Target architecture: three layers

```text
┌─────────────────────────────────────────────────────────┐
│  Brain — flexible coding agent runtime                  │
│  (OpenHands-like loop, condensers, skills, heuristics)  │
└──────────────────────────┬──────────────────────────────┘
                           │ tool calls only
┌──────────────────────────▼──────────────────────────────┐
│  Hands — adapters to Poseidon MCP / approved backends   │
│  (sandbox files/exec/tests, bundle, open_pr, …)         │
└──────────────────────────┬──────────────────────────────┘
                           │ policy-checked, audited
┌──────────────────────────▼──────────────────────────────┐
│  Law — gateway + policy engine + audit + delivery       │
│  (credentials, semantic rules, verify/delivery gates)   │
└─────────────────────────────────────────────────────────┘
```

| Layer | Owns | Must not own |
|---|---|---|
| **Brain** | Reasoning loop, context engineering, coding strategy | Credentials, direct git/network egress, policy source of truth |
| **Hands** | Mapping model tools → gateway MCP calls; hiding plumbing (e.g. bundles) | Bypasses around the gateway |
| **Law** | AuthN/Z, OPA/Cedar decisions, audit, push/PR with platform tokens, configured success gates | Agent prompt text as the sole enforcement mechanism |

The PoC `packages/agent` package is a minimal Brain+Hands fused to a demo Law.
MVP should swap in a stronger Brain while preserving Hands→Law as the only path
to the outside world.

## Framework adoption rules

Adopt (or evaluate) OpenHands-class machinery **as a library behind Poseidon
contracts**, not as a replacement control plane:

- Agent tools resolve to Poseidon MCP (or gateway-proxied MCP), not privileged
  local git/network operations.
- Sandbox lifecycle and credentials stay with runner/gateway/sandbox backends.
- Delivery remains push-via-gateway (e.g. git bundle → policy → platform push).
- Verify/delivery gates stay explicit platform policy, not “whatever the model
  decided to run.”
- If the framework ships its own security analyzer or workspace policy, it is
  defense-in-depth at most — never the source of truth for semantic rules such
  as `feature/*` vs `main`.

## Practical split

| Concern | MVP direction |
|---|---|
| Agent reasoning loop / context engineering | Prefer a mature coding-agent framework |
| Hard governance (credentials, egress, semantic policy, audit, verify gates) | Remains Poseidon’s product surface |
| Tool access | Gateway-mediated; phase or policy may still restrict which tools are available |
| Delivery | Platform-owned; agent never holds remotes/tokens |
| PoC agent package | Keep as the governance proof; treat it as a seam to be replaced, not as the final brain |

## Verdict

For arbitrary future coding work, use a serious agent framework for the loop and
context stack. Keep clear governance around it: Poseidon remains the law —
credential boundary, policy, audit, and delivery — so flexibility in problem
solving never becomes flexibility in bypassing the platform.
