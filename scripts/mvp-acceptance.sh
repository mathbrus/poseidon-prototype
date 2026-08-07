#!/usr/bin/env bash
# Plan step 25 — MVP acceptance: the §10 exit criteria, scripted, in one
# command. The suite is tests/e2e_kind in full; its capstone module
# (test_zz_acceptance.py) documents which module proves which criterion.
#
# The suite itself is cluster-neutral: it reaches the cluster through the
# current kubectl context, deploy/kind/.env, and the gateway URL in it.
# Today it runs against kind (deploy/kind/kind-up.sh is the bring-up);
# §10 criterion 7's real-cluster half is deliberately deferred — when a
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

echo "== MVP acceptance (§10, plan step 25) against $(kubectl config current-context)"
uv run pytest tests/e2e_kind -m e2e_kind "$@"

cat <<'EOF'

== §10 exit criteria: all scripted checks green ==
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
EOF
