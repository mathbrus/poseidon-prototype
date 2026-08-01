#!/usr/bin/env bash
# Poseidon M1 kind bootstrap (plan step 1a/1b): create the cluster, install
# Calico (kind's default CNI does not enforce NetworkPolicy), build and
# side-load the images, bridge the transitional Docker sandbox network into
# the cluster, and install the Helm chart. Idempotent — safe to re-run for
# image or chart updates.
#
# Usage: deploy/kind/kind-up.sh   (needs deploy/kind/.env, see .env.example)
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

CLUSTER_NAME=${CLUSTER_NAME:-poseidon}
PLATFORM_NS=${PLATFORM_NS:-poseidon-platform}
COMPANY_NS=${COMPANY_NS:-hello-saas}
ENV_FILE=${ENV_FILE:-$SCRIPT_DIR/.env}
CALICO_MANIFEST=${CALICO_MANIFEST:-https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml}
TEMPORAL_CHART_VERSION=${TEMPORAL_CHART_VERSION:-1.6.0}

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found — copy $SCRIPT_DIR/.env.example and fill it in" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
SANDBOX_NETWORK=${SANDBOX_NETWORK:-poseidon-kind-sandboxnet}
SANDBOX_IMAGE=${SANDBOX_IMAGE:-poseidon-sandbox}

echo "==> cluster"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  kind create cluster --config "$SCRIPT_DIR/kind-config.yaml"
else
  echo "    kind cluster '$CLUSTER_NAME' already exists"
fi
kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null

echo "==> CNI (Calico — NetworkPolicy enforcement)"
kubectl apply -f "$CALICO_MANIFEST"
kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s
kubectl wait --for=condition=Ready node --all --timeout=300s

echo "==> images"
docker build -f Dockerfile.gateway -t poseidon-gateway .
docker build -f Dockerfile.agent -t poseidon-agent .
docker build -f Dockerfile.sandbox -t "$SANDBOX_IMAGE" .
# The sandbox image is not loaded: sandboxes stay host-side Docker containers
# until the k8s_jobs backend (plan step 4).
kind load docker-image poseidon-gateway poseidon-agent --name "$CLUSTER_NAME"

echo "==> transitional sandbox network"
# Internal (no route out — the sandbox keeps zero egress, E3); the kind node
# joins it so gateway pods can reach sandbox toolservers by container IP.
if ! docker network inspect "$SANDBOX_NETWORK" >/dev/null 2>&1; then
  docker network create --internal "$SANDBOX_NETWORK"
fi
if ! docker network inspect "$SANDBOX_NETWORK" \
    --format '{{range .Containers}}{{.Name}} {{end}}' \
    | grep -qw "${CLUSTER_NAME}-control-plane"; then
  docker network connect "$SANDBOX_NETWORK" "${CLUSTER_NAME}-control-plane"
fi

echo "==> helm install"
# Secrets ride a private temp values file, not --set, to keep tokens off the
# process list.
SECRETS_FILE=$(mktemp)
trap 'rm -f "$SECRETS_FILE"' EXIT
cat > "$SECRETS_FILE" <<EOF
secrets:
  gitToken: "${GIT_TOKEN:?GIT_TOKEN missing from $ENV_FILE}"
  llmApiKey: "${LLM_API_KEY:?LLM_API_KEY missing from $ENV_FILE}"
  agentBearerToken: "${AGENT_BEARER_TOKEN:?AGENT_BEARER_TOKEN missing from $ENV_FILE}"
  runnerBearerToken: "${RUNNER_BEARER_TOKEN:?RUNNER_BEARER_TOKEN missing from $ENV_FILE}"
  postgresPassword: "${POSTGRES_PASSWORD:-poseidon-dev-postgres}"
run:
  model: "${MODEL:?MODEL missing from $ENV_FILE}"
  targetRepo: "${TARGET_REPO:?TARGET_REPO missing from $ENV_FILE}"
  baseRef: "${BASE_REF:-main}"
  maxIterations: ${MAX_ITERATIONS:-10}
EOF
helm upgrade --install poseidon deploy/helm/poseidon \
  --namespace "$PLATFORM_NS" --create-namespace \
  -f deploy/helm/poseidon/values-kind.yaml \
  -f "$SECRETS_FILE" \
  --set-file opa.policies.poc=policies/poc.rego

echo "==> rollout"
for deploy in opa llm-proxy mcp-gateway; do
  kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=300s
done
kubectl -n "$PLATFORM_NS" rollout status statefulset/postgres --timeout=300s
kubectl -n "$COMPANY_NS" rollout status deployment/agent --timeout=300s

echo "==> temporal (chart $TEMPORAL_CHART_VERSION)"
# Version-pinned, same values path for kind and real clusters (plan step 2).
# The schema hook jobs create/migrate both databases against the poseidon
# chart's Postgres, which is Ready by now.
if ! helm repo list 2>/dev/null | grep -q go.temporal.io; then
  helm repo add temporal https://go.temporal.io/helm-charts
fi
helm upgrade --install temporal temporal/temporal \
  --version "$TEMPORAL_CHART_VERSION" \
  --namespace "$PLATFORM_NS" \
  -f deploy/helm/temporal-values.yaml \
  --timeout 15m
for deploy in temporal-frontend temporal-history temporal-matching temporal-worker temporal-web; do
  kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=600s
done

echo "==> smoke"
# No unauthenticated gateway route exists; 401 proves the server is up.
code=$(curl -s -o /dev/null -w '%{http_code}' "${MCP_GATEWAY_URL:-http://127.0.0.1:8080}/mcp")
if [ "$code" != "401" ]; then
  echo "error: gateway answered $code, expected 401 without a token" >&2
  exit 1
fi
curl -fs "${AGENT_URL:-http://127.0.0.1:8000}/openapi.json" >/dev/null

echo "==> up: gateway ${MCP_GATEWAY_URL:-http://127.0.0.1:8080}, agent ${AGENT_URL:-http://127.0.0.1:8000}"
