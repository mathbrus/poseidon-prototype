#!/usr/bin/env bash
# Runs the acceptance suite (tests/e2e_kind in full) in one command.
# Its capstone module (tests/e2e_kind/test_acceptance.py) is the index of
# which module proves which claim.
#
# The suite is cluster-neutral: it reaches the cluster through the current
# kubectl context, deploy/kind/.env, and the gateway and console URLs in
# it; deploy/kind/kind-up.sh is the kind bring-up. Every act on the company
# is the console's, as the human the chart declares.
#
# Cost: zero LLM spend, enforced — the preflight below and the suite's own
# session guard both refuse a paid brain (read from the deployed llm-proxy
# image, never from .env's REAL_LLM claim) unless POSEIDON_ALLOW_PAID=1
# opts into spending deliberately. Opens real PRs on the disposable
# TARGET_REPO from deploy/kind/.env; the suite seeds, sweeps, and asserts
# the repo and cluster quiet itself (tests/e2e_kind/conftest.py documents
# the contract).
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
CONSOLE_URL=$(grep -E '^POSEIDON_CONSOLE_URL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
CONSOLE_URL=${CONSOLE_URL:-http://127.0.0.1:8090}
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$CONSOLE_URL/whoami" || true)
[ "$code" = "401" ] || {
  echo "console at $CONSOLE_URL answered '$code' (expected 401 without a token)" >&2
  exit 1
}
# The LLM-cost rule, enforced at the door: the suite's own session guard
# (tests/e2e_kind/conftest.py, paid_brain_guard) refuses a paid brain too,
# but failing here is seconds cheaper. One rule of record — the guard's
# pure function — applied to the *deployment*, never .env's REAL_LLM claim.
BRAIN=$(kubectl -n poseidon-platform get deployment/llm-proxy \
  -o 'jsonpath={.spec.template.spec.containers[0].image}' 2>/dev/null || true)
PROBLEM=$(uv run python -c '
import sys
from tests.e2e_kind.conftest import paid_brain_problem
print(paid_brain_problem(sys.argv[1], sys.argv[2] or None) or "")
' "$BRAIN" "${POSEIDON_ALLOW_PAID:-}")
if [ -n "$PROBLEM" ]; then
  echo "$PROBLEM" >&2
  exit 1
fi
case "$BRAIN" in
*mockllm*) ;;
*) echo "== PAID brain ($BRAIN): POSEIDON_ALLOW_PAID=1 set, spending deliberately" ;;
esac

echo "== Acceptance suite against $(kubectl config current-context)"
uv run pytest tests/e2e_kind -m e2e_kind --durations=25 "$@"

echo "== green: tests/e2e_kind/test_acceptance.py indexes which module proved what"
