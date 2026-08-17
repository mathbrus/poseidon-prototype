#!/usr/bin/env bash
# Poseidon kind bootstrap: create the cluster, install
# Calico (kind's default CNI does not enforce NetworkPolicy), build and
# side-load the images, and install the Helm chart. Idempotent — safe to
# re-run for image or chart updates.
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
SANDBOX_IMAGE=${SANDBOX_IMAGE:-poseidon-sandbox}
# The LLM is mocked by default so runs cost nothing; REAL_LLM=1 deploys the
# real LiteLLM proxy and then requires LLM_API_KEY and MODEL.
REAL_LLM=${REAL_LLM:-0}
# The Temporal web UI is a browsable surface over full operator authority
# (see README §9); it stays out of the cluster unless explicitly asked for.
TEMPORAL_WEB=${TEMPORAL_WEB:-0}
if [ "$REAL_LLM" != "1" ]; then
  LLM_API_KEY=${LLM_API_KEY:-mock-unused}
  MODEL=mock-brain
else
  : "${LLM_API_KEY:?REAL_LLM=1 requires LLM_API_KEY in $ENV_FILE}"
  : "${MODEL:?REAL_LLM=1 requires MODEL in $ENV_FILE (e.g. gpt-5.6-luna)}"
fi

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
# Content-addressed tag over everything that lands in an image. This is what
# makes a code change reach the cluster: with a fixed `:latest` tag, `kind
# load` replaces the image in the node but helm renders an identical pod
# template, so nothing restarts and pods keep executing the code they
# started with — silently, for as long as you let them. A tag that moves
# with the content changes the template, which forces the rollout; an
# unchanged tree yields the same tag and stays a no-op, so this script
# remains idempotent.
BUILD_TAG=${BUILD_TAG:-$(
  find packages Dockerfile.* pyproject.toml uv.lock -type f \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -print0 \
    | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-12
)}
echo "    build tag: $BUILD_TAG"

docker build -q -f Dockerfile.gateway -t "poseidon-gateway:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.agent -t "poseidon-agent:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.orchestrator -t "poseidon-orchestrator:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.sandbox -t "$SANDBOX_IMAGE:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.egressproxy -t "poseidon-egress-proxy:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.operator -t "poseidon-operator:$BUILD_TAG" . >/dev/null
docker build -q -f Dockerfile.gatewayllm -t "poseidon-gateway-llm:$BUILD_TAG" . >/dev/null
# The sandbox image is side-loaded too: sandboxes are Kubernetes Jobs in the
# company namespace, run by the k8s_jobs backend.
IMAGES=(
  "poseidon-gateway:$BUILD_TAG"
  "poseidon-agent:$BUILD_TAG"
  "poseidon-orchestrator:$BUILD_TAG"
  "$SANDBOX_IMAGE:$BUILD_TAG"
  "poseidon-egress-proxy:$BUILD_TAG"
  "poseidon-operator:$BUILD_TAG"
  "poseidon-gateway-llm:$BUILD_TAG"
)
IMAGE_VALUES=(
  --set "images.gateway.tag=$BUILD_TAG"
  --set "images.agent.tag=$BUILD_TAG"
  --set "images.orchestrator.tag=$BUILD_TAG"
  --set "egressProxy.image.tag=$BUILD_TAG"
  --set "images.operator.tag=$BUILD_TAG"
  --set "images.gatewayLlm.tag=$BUILD_TAG"
  # Sandbox Jobs are built by the worker from this value, not by helm, so
  # the tag has to ride the image reference itself.
  --set "orchestrator.sandbox.image=$SANDBOX_IMAGE:$BUILD_TAG"
)
if [ "$REAL_LLM" != "1" ]; then
  docker build -q -f Dockerfile.mockllm -t "poseidon-mockllm:$BUILD_TAG" . >/dev/null
  IMAGES+=("poseidon-mockllm:$BUILD_TAG")
  # Only in mock mode: with REAL_LLM=1 the llm-proxy image is upstream
  # LiteLLM at its own pinned tag.
  IMAGE_VALUES+=(--set "images.llmProxy.tag=$BUILD_TAG")
else
  # The chart's images.llmProxy values are the single source of truth for
  # which LiteLLM the cluster runs; read them rather than duplicating the
  # tag here.
  VALUES_FILE=deploy/helm/poseidon/values.yaml
  LITELLM_REPO=$(awk '/^  llmProxy:/{f=1} f && /^    repository:/{gsub(/"/,"",$2); print $2; exit}' "$VALUES_FILE")
  LITELLM_TAG=$(awk '/^  llmProxy:/{f=1} f && /^    tag:/{gsub(/"/,"",$2); print $2; exit}' "$VALUES_FILE")
  if [ -z "$LITELLM_REPO" ] || [ -z "$LITELLM_TAG" ]; then
    echo "error: could not read images.llmProxy from $VALUES_FILE" >&2
    exit 1
  fi
  LITELLM_IMAGE="$LITELLM_REPO:$LITELLM_TAG"
  docker pull "$LITELLM_IMAGE"
  kind load docker-image "$LITELLM_IMAGE" --name "$CLUSTER_NAME"
fi
kind load docker-image "${IMAGES[@]}" --name "$CLUSTER_NAME"

echo "==> crds"
# CRDs are cluster-scoped and outlive any release: applied directly rather
# than templated, so a `helm uninstall` cannot take a company's schema with it.
kubectl apply -f crds/

echo "==> helm install"
# Secrets ride a private temp values file, not --set, to keep tokens off the
# process list.
SECRETS_FILE=$(mktemp)
trap 'rm -f "$SECRETS_FILE"' EXIT
cat > "$SECRETS_FILE" <<EOF
secrets:
  gitToken: "${GIT_TOKEN:?GIT_TOKEN missing from $ENV_FILE}"
  llmApiKey: "${LLM_API_KEY:?LLM_API_KEY missing from $ENV_FILE}"
  postgresPassword: "${POSTGRES_PASSWORD:-poseidon-dev-postgres}"
  egressProxyKey: "${EGRESS_PROXY_KEY:-poseidon-dev-egress-key}"
run:
  model: "${MODEL:?MODEL missing from $ENV_FILE}"
  targetRepo: "${TARGET_REPO:?TARGET_REPO missing from $ENV_FILE}"
  baseRef: "${BASE_REF:-main}"
  maxIterations: ${MAX_ITERATIONS:-10}
# LiteLLM serves one model; the gateway overwrites the request's model
# from the caller's tier, so every tier must name that same model.
llmGateway:
  tierModels:
    frontier: "${MODEL}"
    standard: "${MODEL}"
    small: "${MODEL}"
EOF
MOCK_VALUES=()
if [ "$REAL_LLM" != "1" ]; then
  MOCK_VALUES=(-f deploy/helm/poseidon/values-mockllm.yaml)
fi
helm upgrade --install poseidon deploy/helm/poseidon \
  --namespace "$PLATFORM_NS" --create-namespace \
  -f deploy/helm/poseidon/values-kind.yaml \
  -f "$SECRETS_FILE" \
  "${MOCK_VALUES[@]}" \
  "${IMAGE_VALUES[@]}"

echo "==> rollout"
# Postgres first: every service migrates the registry schema at startup and
# the bundle server / operator need it reachable before they are useful.
kubectl -n "$PLATFORM_NS" rollout status statefulset/postgres --timeout=300s
for deploy in opa llm-proxy llm-gateway mcp-gateway egress-proxy bundle-server poseidon-operator; do
  kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=300s
done

echo "==> temporal (chart $TEMPORAL_CHART_VERSION)"
# Version-pinned, same values path for kind and real clusters.
# The schema hook jobs create/migrate both databases against the poseidon
# chart's Postgres, which is Ready by now.
if ! helm repo list 2>/dev/null | grep -q go.temporal.io; then
  helm repo add temporal https://go.temporal.io/helm-charts
fi
TEMPORAL_DEPLOYS=(temporal-frontend temporal-history temporal-matching temporal-worker)
TEMPORAL_VALUES=()
if [ "$TEMPORAL_WEB" = "1" ]; then
  TEMPORAL_VALUES=(--set web.enabled=true)
  TEMPORAL_DEPLOYS+=(temporal-web)
fi
helm upgrade --install temporal temporal/temporal \
  --version "$TEMPORAL_CHART_VERSION" \
  --namespace "$PLATFORM_NS" \
  -f deploy/helm/temporal-values.yaml \
  "${TEMPORAL_VALUES[@]}" \
  --timeout 15m
for deploy in "${TEMPORAL_DEPLOYS[@]}"; do
  kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=600s
done
kubectl -n "$PLATFORM_NS" rollout status deployment/orchestrator-worker --timeout=300s

echo "==> apply the blueprint"
# The company is declarative from here: compile + submit CRs, and the
# operator converges the namespace, its policies and the standing roster.
# A port-forward lets apply read the live registry, so the diff it prints is
# the real one rather than "everything is new".
kubectl -n "$PLATFORM_NS" port-forward svc/postgres 15432:5432 >/dev/null 2>&1 &
PGPF=$!
trap 'rm -f "$SECRETS_FILE"; kill $PGPF 2>/dev/null || true' EXIT
for _ in $(seq 30); do
  (exec 3<>/dev/tcp/127.0.0.1/15432) 2>/dev/null && break
  sleep 1
done
REGISTRY_DSN="postgresql://${POSTGRES_USER:-poseidon}:${POSTGRES_PASSWORD:-poseidon-dev-postgres}@127.0.0.1:15432/${POSTGRES_USER:-poseidon}" \
  uv run poseidon blueprint apply examples/hello-saas.yaml --yes
kill $PGPF 2>/dev/null || true
# Lean roster: the PM is the only standing hire; execution
# is ephemeral crews.
for hire in pm; do
  kubectl -n "$COMPANY_NS" rollout status "deployment/$hire" --timeout=300s
done

echo "==> image check"
# Assert every poseidon pod actually runs this build. Without it, a template
# that forgets the tag value puts stale code back in the cluster and the
# only symptom is a test failing for a bug you already fixed.
# Pods on their way out (a superseded ReplicaSet still draining) and
# finished sandbox Jobs are not "running stale code" — only live ones count.
# Fields are pipe-delimited so an absent deletionTimestamp stays an empty
# column rather than shifting the row.
stale=$(
  kubectl get pods -A \
    -o jsonpath='{range .items[*]}{.status.phase}|{.metadata.deletionTimestamp}|{.metadata.namespace}/{.metadata.name}|{range .spec.containers[*]}{.image} {end}{"\n"}{end}' \
    | awk -F'|' -v tag=":$BUILD_TAG" '
        $1 == "Succeeded" || $1 == "Failed" { next }   # finished sandbox Jobs
        $2 != ""                            { next }   # terminating pods
        $4 ~ /poseidon-[a-z-]*:/ && index($4, tag) == 0 { print $3, $4 }
      '
)
if [ -n "$stale" ]; then
  echo "error: pods not running build $BUILD_TAG:" >&2
  echo "$stale" >&2
  exit 1
fi
echo "    all poseidon pods run $BUILD_TAG"

echo "==> smoke"
# No unauthenticated gateway route exists; 401 proves the server is up.
code=$(curl -s -o /dev/null -w '%{http_code}' "${MCP_GATEWAY_URL:-http://127.0.0.1:8080}/mcp")
if [ "$code" != "401" ]; then
  echo "error: gateway answered $code, expected 401 without a token" >&2
  exit 1
fi
# Standing agents have no NodePort: they are reached in-cluster
# by the orchestrator, and the operator gives each hire its own Service.

echo "==> up: gateway ${MCP_GATEWAY_URL:-http://127.0.0.1:8080}"
