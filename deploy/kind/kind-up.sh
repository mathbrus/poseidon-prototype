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
# An explicit invocation-time REAL_LLM must beat the .env: whether a run
# spends money is a per-invocation decision, and `REAL_LLM=0 kind-up.sh`
# silently losing to a forgotten REAL_LLM=1 in the file is how a mock test
# run ends up billing a provider.
REAL_LLM_ARG=${REAL_LLM-}
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
SANDBOX_IMAGE=${SANDBOX_IMAGE:-poseidon-sandbox}
# The LLM is mocked by default so runs cost nothing; REAL_LLM=1 deploys the
# real LiteLLM proxy and then requires LLM_API_KEY and MODEL.
REAL_LLM=${REAL_LLM_ARG:-${REAL_LLM:-0}}
# The Temporal web UI is a browsable surface over full operator authority;
# it stays out of the cluster unless explicitly asked for.
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
  find packages Dockerfile deploy/build-images.sh pyproject.toml uv.lock -type f \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -print0 \
    | sort -z | xargs -0 sha1sum | sha1sum | cut -c1-12
)}
echo "    build tag: $BUILD_TAG"

# The mock brain is an image of ours too; with REAL_LLM=1 the llm-proxy
# runs upstream LiteLLM at its own pinned tag instead.
NAMES=$(deploy/build-images.sh --list)
if [ "$REAL_LLM" = "1" ]; then
  NAMES=$(grep -vx poseidon-mockllm <<<"$NAMES")
fi
# shellcheck disable=SC2086
mapfile -t IMAGES < <(deploy/build-images.sh "$BUILD_TAG" $NAMES)
# Sandboxes are Kubernetes Jobs in the company namespace, run by the
# k8s_jobs backend from the image name the worker is configured with.
if [ "$SANDBOX_IMAGE" != "poseidon-sandbox" ]; then
  docker tag "poseidon-sandbox:$BUILD_TAG" "$SANDBOX_IMAGE:$BUILD_TAG"
  IMAGES+=("$SANDBOX_IMAGE:$BUILD_TAG")
fi
# Every image the chart lists under `images:` that is ours (a poseidon-*
# repository) gets this build's tag; third-party images keep their pins.
IMAGE_VALUES=(
  # Sandbox Jobs are built by the worker from this value, not by helm, so
  # the tag has to ride the image reference itself.
  --set "orchestrator.sandbox.image=$SANDBOX_IMAGE:$BUILD_TAG"
)
for key in $(uv run python -c '
import yaml
images = yaml.safe_load(open("deploy/helm/poseidon/values.yaml"))["images"]
print(" ".join(k for k, v in images.items() if v["repository"].startswith("poseidon-")))
'); do
  IMAGE_VALUES+=(--set "images.$key.tag=$BUILD_TAG")
done
if [ "$REAL_LLM" != "1" ]; then
  IMAGE_VALUES+=(--set "images.llmProxy.tag=$BUILD_TAG")
else
  # The chart's images.llmProxy values are the single source of truth for
  # which LiteLLM the cluster runs; read them rather than duplicating the
  # tag here.
  LITELLM_IMAGE=$(uv run python -c '
import yaml
image = yaml.safe_load(open("deploy/helm/poseidon/values.yaml"))["images"]["llmProxy"]
print(image["repository"] + ":" + image["tag"])
')
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
# operator converges the namespace, its policies and the standing agents.
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
# Every standing agent the applied blueprint hires (execution itself is
# ephemeral crews, so these are the only long-lived company pods).
for hire in $(kubectl -n "$COMPANY_NS" get agentinstances -o jsonpath='{.items[*].spec.standingName}'); do
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
# by the orchestrator, and the operator gives each standing agent its own Service.

echo "==> up: gateway ${MCP_GATEWAY_URL:-http://127.0.0.1:8080}"
