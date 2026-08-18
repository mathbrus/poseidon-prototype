#!/usr/bin/env bash
# Runs the MVP acceptance suite (tests/e2e_kind in full) in one command.
# Its capstone module (test_zz_acceptance.py) documents which module
# proves which criterion.
#
# The suite itself is cluster-neutral: it reaches the cluster through the
# current kubectl context, deploy/kind/.env, and the gateway URL in it.
# Today it runs against kind (deploy/kind/kind-up.sh is the bring-up); the
# real-cluster run is deliberately deferred (kind-only for now) — when a
# real cluster exists, the intent is that this same script runs unchanged
# against its context.
#
# Cost: zero LLM spend — the cluster deploys the deterministic mock brain
# unless it was brought up with REAL_LLM=1. Opens real PRs on the
# disposable TARGET_REPO from deploy/kind/.env.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=deploy/kind/.env
GATEWAY_URL=$(grep -E '^MCP_GATEWAY_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
GATEWAY_URL=${GATEWAY_URL:-http://127.0.0.1:8080}

# Preflight, so a missing cluster fails in seconds with a useful message
# rather than minutes into the first fixture.
[ -f "$ENV_FILE" ] || {
  echo "no $ENV_FILE — copy deploy/kind/.env.example and fill it in" >&2
  exit 1
}
kubectl get deployment/orchestrator-worker -n poseidon-platform >/dev/null 2>&1 || {
  echo "cluster unreachable via current kubectl context — deploy/kind/kind-up.sh?" >&2
  exit 1
}
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$GATEWAY_URL/mcp" || true)
[ "$code" = "401" ] || {
  echo "gateway at $GATEWAY_URL answered '$code' (expected 401 without a token)" >&2
  exit 1
}

echo "== MVP acceptance against $(kubectl config current-context)"
uv run pytest tests/e2e_kind -m e2e_kind "$@"

cat <<'EOF'

== Acceptance criteria: all scripted checks green ==
 1 declarative lifecycle   compile/preview/apply/edit/teardown+revocation
 2 durable                 kill-worker resume; nothing leaked (sandbox,
                           identity, participant row, registration)
 3 end-to-end SDLC         intake -> implement -> review -> gated merge
 4 governance re-proof     compiled-policy denies, injected conductor,
                           egress containment, five real audit sources
 5 budgets                 hard-stop, visible pause, escalation, full
                           {run, parent, participant, role} attribution
 6 standing/ephemeral      one standing AgentInstance (CRs and registry),
                           scope denies live for PM and executor
 7 kind/real parity        kind half only — real-cluster run deferred
 8 unattended loop         conduct heartbeat runs the backlog: one goal in,
                           one merge approval out; caps + revocation held;
                           attacker-filed issue dies at the gates
 9 merge under policy      delegated gate: in-class passing work merges with
                           zero human commands (test_zz_delegated); steered
                           reviewer + carve-out PR refused by the platform
                           (test_merge_gate_redteam)
EOF
