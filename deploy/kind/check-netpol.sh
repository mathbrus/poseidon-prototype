#!/usr/bin/env bash
# NetworkPolicy probe: from inside the agent pod,
# the two gateways answer and nothing else does; no pod in the company
# namespace carries a credential-shaped env var.
set -euo pipefail

PLATFORM_NS=${PLATFORM_NS:-poseidon-platform}
COMPANY_NS=${COMPANY_NS:-hello-saas}

AGENT_POD=$(kubectl -n "$COMPANY_NS" get pod -l poseidon.dev/kind=standing-agent \
  -o jsonpath='{.items[0].metadata.name}')
echo "==> probing from $COMPANY_NS/$AGENT_POD"

# Any HTTP answer (401 included) counts as reachable; "blocked" must fail at
# the transport layer within the timeout.
PROBE='
import sys, urllib.request, urllib.error
url, want = sys.argv[1], sys.argv[2]
try:
    urllib.request.urlopen(url, timeout=3)
    reachable = True
except urllib.error.HTTPError:
    reachable = True
except Exception:
    reachable = False
sys.exit(0 if reachable == (want == "reachable") else 1)
'

probe() {
  local url=$1 want=$2
  if kubectl -n "$COMPANY_NS" exec "$AGENT_POD" -- python -c "$PROBE" "$url" "$want"; then
    echo "    ok: $url is $want"
  else
    echo "    FAIL: $url should be $want" >&2
    exit 1
  fi
}

probe "http://mcp-gateway.$PLATFORM_NS.svc.cluster.local:8080/mcp" reachable
probe "http://llm-gateway.$PLATFORM_NS.svc.cluster.local:4000/health" reachable
# The llm-proxy is sealed to everyone but the LLM gateway; from an agent
# pod it must fail at the transport layer (mirrors the e2e containment test).
probe "http://llm-proxy.$PLATFORM_NS.svc.cluster.local:4000/health/liveliness" blocked
probe "http://opa.$PLATFORM_NS.svc.cluster.local:8181/health" blocked
probe "http://1.1.1.1" blocked
probe "http://api.github.com" blocked

echo "==> scanning $COMPANY_NS pod envs for credential-shaped variables"
# Same rule as the CI secret-inspection gate: nothing *_TOKEN/*_KEY
# (a standing agent's credential is a projected token file);
# GPG_KEY only if blanked. Crew pods carry their minted
# ephemeral identity as GATEWAY_TOKEN — run-scoped and registry-revocable,
# deliberately exempt.
for pod in $(kubectl -n "$COMPANY_NS" get pods -o jsonpath='{.items[*].metadata.name}'); do
  kind=$(kubectl -n "$COMPANY_NS" get pod "$pod" \
    -o jsonpath='{.metadata.labels.poseidon\.dev/kind}')
  exempt='^GPG_KEY=$'
  if [ "$kind" = "crew-agent" ]; then exempt='^GPG_KEY=$|^GATEWAY_TOKEN='; fi
  leaked=$(kubectl -n "$COMPANY_NS" exec "$pod" -- printenv \
    | grep -E '^[A-Za-z_]*_(TOKEN|KEY)=' \
    | grep -vE "$exempt" || true)
  if [ -n "$leaked" ]; then
    echo "    FAIL: credential-shaped env vars in $pod:" >&2
    echo "$leaked" | sed 's/=.*/=…/' >&2
    exit 1
  fi
  echo "    ok: $pod env is clean"
done

echo "==> NetworkPolicy probe passed"
