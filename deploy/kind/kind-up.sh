#!/usr/bin/env bash
# Poseidon kind bootstrap: create the cluster, install Calico (kind's
# default CNI does not enforce NetworkPolicy), build and side-load the
# images, install the Helm chart and Temporal, and log you in. It installs
# the platform and knows nothing about any company: applying a blueprint is
# a human's act, done afterwards with `poseidon blueprint apply`. Idempotent
# — safe to re-run for image or chart updates; a company already applied
# stays applied, and its standing agents re-roll onto the new build.
#
# Usage: deploy/kind/kind-up.sh   (needs deploy/kind/.env, see .env.example)
#        VERBOSE=1 deploy/kind/kind-up.sh   (stream every command's output)
#
# What it prints is the progress: one line per step, with the time each
# took, and one line per fact worth knowing. What the tools print — kind,
# kubectl, helm, docker — goes to the log (deploy/kind/kind-up.log), whose
# tail is shown when a step fails.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

CLUSTER_NAME=${CLUSTER_NAME:-poseidon}
# The platform namespace is `common.topology.PLATFORM_NAMESPACE`, read
# once the tools are known to be present (in the preflight).
ENV_FILE=${ENV_FILE:-$SCRIPT_DIR/.env}
LOG=${KIND_UP_LOG:-$SCRIPT_DIR/kind-up.log}
VERBOSE=${VERBOSE:-0}
CALICO_MANIFEST=${CALICO_MANIFEST:-https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml}
TEMPORAL_CHART_VERSION=${TEMPORAL_CHART_VERSION:-1.6.0}

# -- progress ------------------------------------------------------------------
# Eight steps, always eight: conditional work stays inside its step so the
# count never lies. `step` opens one (closing the previous with its time),
# `note` adds a fact under it, `run` executes a noisy command into the log
# and speaks up only when it has been silent for a while.
STEPS=8
STEP=0
STEP_NAME=""
STEP_STARTED=$SECONDS
SECRETS_FILE=""

step() {
  close_step
  STEP=$((STEP + 1))
  STEP_NAME=$1
  STEP_STARTED=$SECONDS
  printf '[%d/%d] %s\n' "$STEP" "$STEPS" "$STEP_NAME"
  printf '\n==== [%d/%d] %s\n' "$STEP" "$STEPS" "$STEP_NAME" >>"$LOG"
}

close_step() {
  if [ "$STEP" -gt 0 ]; then
    printf '      done in %ss\n' "$((SECONDS - STEP_STARTED))"
  fi
}

note() {
  printf '      %s\n' "$*"
}

# `run label command…`: the command's output lands in the log. After 20 s
# of silence a line says the step is still going, every 15 s. Commands
# whose stdout is data (the image list, the values reads) are not wrapped:
# they are read by the script, and only their stderr belongs in the log.
run() {
  local label=$1
  shift
  printf '\n### %s: %s\n' "$label" "$*" >>"$LOG"
  if [ "$VERBOSE" = "1" ]; then
    "$@" 2>&1 | tee -a "$LOG"
    return "${PIPESTATUS[0]}"
  fi
  "$@" >>"$LOG" 2>&1 &
  local pid=$! started=$SECONDS spoken=0 waited
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((SECONDS - started))
    if [ "$waited" -ge 20 ] && [ $((waited - spoken)) -ge 15 ]; then
      spoken=$waited
      printf '      … %s (%ss)\n' "$label" "$waited"
    fi
  done
  wait "$pid"
}

fail() {
  echo "error: $*" >&2
  exit 1
}

cleanup() {
  local status=$?
  [ -n "$SECRETS_FILE" ] && rm -f "$SECRETS_FILE"
  if [ "$status" -ne 0 ]; then
    echo "error: kind-up failed in step $STEP/$STEPS ($STEP_NAME); the last lines of $LOG:" >&2
    tail -n 40 "$LOG" >&2 2>/dev/null || true
  fi
}
trap cleanup EXIT

: >"$LOG"
STARTED=$SECONDS

# -- 1. preflight ----------------------------------------------------------------
step "preflight"
note "log: $LOG"
missing=()
for tool in kind kubectl helm docker uv curl; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
[ "${#missing[@]}" -eq 0 ] || fail "missing tools: ${missing[*]}"
PLATFORM_NS=$(uv run python -c 'from common.topology import PLATFORM_NAMESPACE; print(PLATFORM_NAMESPACE)')
docker_root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null) || fail "docker is not reachable"
free_gib=$(df -BG --output=avail "$docker_root" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$free_gib" ]; then
  if [ "$free_gib" -lt 10 ]; then
    note "warning: ${free_gib} GiB free under $docker_root — kind and the images need room (\`docker system df\`, \`docker image prune\`)"
  else
    note "docker: ${free_gib} GiB free under $docker_root"
  fi
fi

[ -f "$ENV_FILE" ] || fail "$ENV_FILE not found — copy $SCRIPT_DIR/.env.example and fill it in"
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
: "${GIT_TOKEN:?GIT_TOKEN missing from $ENV_FILE}"

# Who a person can be on this cluster: the chart's kind humans, each a
# `human-<name>` ServiceAccount the console verifies. The first is who
# this script logs you in as. A blueprint may only name people from this
# list; the console refuses any other name at apply.
read -r -a HUMANS <<<"$(uv run python -c '
import yaml
print(" ".join(yaml.safe_load(open("deploy/helm/poseidon/values-kind.yaml"))["console"]["humans"]))
')"
KIND_HUMAN=${HUMANS[0]}
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
note "cluster $CLUSTER_NAME, platform namespace $PLATFORM_NS, build tag $BUILD_TAG"
note "humans: ${HUMANS[*]} — you will be $KIND_HUMAN"
if [ "$REAL_LLM" = "1" ]; then
  note "brain: PAID — $MODEL through LiteLLM (REAL_LLM=1)"
else
  note "brain: mock (poseidon-mockllm), zero spend"
fi
note "sandbox image: $SANDBOX_IMAGE:$BUILD_TAG"

# -- 2. cluster ------------------------------------------------------------------
step "cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  note "kind cluster '$CLUSTER_NAME' exists, reused"
else
  run "kind create cluster" kind create cluster --config "$SCRIPT_DIR/kind-config.yaml"
  note "kind cluster '$CLUSTER_NAME' created"
fi
kubectl config use-context "kind-$CLUSTER_NAME" >>"$LOG" 2>&1
# A company already applied is none of this script's business; it is
# only worth a word, since its standing agents will re-roll onto this
# build once the operator restarts with the new image.
existing=$(kubectl get companies -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
[ -z "$existing" ] || note "the cluster hosts $existing; it stays applied"

# -- 3. CNI ----------------------------------------------------------------------
step "CNI (Calico, so NetworkPolicy is enforced)"
run "calico apply" kubectl apply -f "$CALICO_MANIFEST"
run "calico rollout" kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
run "calico controllers" kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s
run "nodes ready" kubectl wait --for=condition=Ready node --all --timeout=300s

# -- 4. images -------------------------------------------------------------------
step "images"
# The mock brain is an image of ours too; with REAL_LLM=1 the llm-proxy
# runs upstream LiteLLM at its own pinned tag instead.
NAMES=$(deploy/build-images.sh --list)
if [ "$REAL_LLM" = "1" ]; then
  NAMES=$(grep -vx poseidon-mockllm <<<"$NAMES")
fi
# shellcheck disable=SC2086
mapfile -t IMAGES < <(deploy/build-images.sh "$BUILD_TAG" $NAMES 2>>"$LOG")
# Sandboxes are Kubernetes Jobs in the company namespace, run by the
# k8s_jobs backend from the image name the worker is configured with.
if [ "$SANDBOX_IMAGE" != "poseidon-sandbox" ]; then
  docker tag "poseidon-sandbox:$BUILD_TAG" "$SANDBOX_IMAGE:$BUILD_TAG"
  IMAGES+=("$SANDBOX_IMAGE:$BUILD_TAG")
fi
note "${#IMAGES[@]} images built at $BUILD_TAG"
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
  run "docker pull litellm" docker pull "$LITELLM_IMAGE"
  run "kind load litellm" kind load docker-image "$LITELLM_IMAGE" --name "$CLUSTER_NAME"
fi
run "kind load images" kind load docker-image "${IMAGES[@]}" --name "$CLUSTER_NAME"
note "loaded into the cluster"

# -- 5. chart --------------------------------------------------------------------
step "chart (CRDs, then the Poseidon release)"
# CRDs are cluster-scoped and outlive any release: applied directly rather
# than templated, so a `helm uninstall` cannot take a company's schema with it.
run "crds" kubectl apply -f crds/
# Secrets ride a private temp values file, not --set, to keep tokens off the
# process list.
SECRETS_FILE=$(mktemp)
cat >"$SECRETS_FILE" <<EOF
secrets:
  gitToken: "${GIT_TOKEN}"
  llmApiKey: "${LLM_API_KEY}"
  postgresPassword: "${POSTGRES_PASSWORD:-poseidon-dev-postgres}"
  egressProxyKey: "${EGRESS_PROXY_KEY:-poseidon-dev-egress-key}"
run:
  model: "${MODEL}"
EOF
MOCK_VALUES=()
if [ "$REAL_LLM" != "1" ]; then
  MOCK_VALUES=(-f deploy/helm/poseidon/values-mockllm.yaml)
fi
run "helm upgrade poseidon" helm upgrade --install poseidon deploy/helm/poseidon \
  --namespace "$PLATFORM_NS" --create-namespace \
  -f deploy/helm/poseidon/values-kind.yaml \
  -f "$SECRETS_FILE" \
  "${MOCK_VALUES[@]}" \
  "${IMAGE_VALUES[@]}"
note "release poseidon installed; humans declared: ${HUMANS[*]}"

# -- 6. platform rollout -----------------------------------------------------------
step "platform rollout"
# Postgres first: every service migrates the registry schema at startup and
# the bundle server / operator need it reachable before they are useful.
run "postgres" kubectl -n "$PLATFORM_NS" rollout status statefulset/postgres --timeout=300s
for deploy in opa llm-proxy llm-gateway mcp-gateway egress-proxy bundle-server poseidon-operator; do
  run "$deploy" kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=300s
done
note "postgres, opa, llm-proxy, llm-gateway, mcp-gateway, egress-proxy, bundle-server, operator ready"

# -- 7. Temporal ---------------------------------------------------------------------
step "Temporal (chart $TEMPORAL_CHART_VERSION)"
# Version-pinned, same values path for kind and real clusters.
# The schema hook jobs create/migrate both databases against the poseidon
# chart's Postgres, which is Ready by now.
if ! helm repo list 2>/dev/null | grep -q go.temporal.io; then
  run "helm repo add temporal" helm repo add temporal https://go.temporal.io/helm-charts
fi
TEMPORAL_DEPLOYS=(temporal-frontend temporal-history temporal-matching temporal-worker)
TEMPORAL_VALUES=()
if [ "$TEMPORAL_WEB" = "1" ]; then
  TEMPORAL_VALUES=(--set web.enabled=true)
  TEMPORAL_DEPLOYS+=(temporal-web)
fi
run "helm upgrade temporal" helm upgrade --install temporal temporal/temporal \
  --version "$TEMPORAL_CHART_VERSION" \
  --namespace "$PLATFORM_NS" \
  -f deploy/helm/temporal-values.yaml \
  "${TEMPORAL_VALUES[@]}" \
  --timeout 15m
for deploy in "${TEMPORAL_DEPLOYS[@]}"; do
  run "$deploy" kubectl -n "$PLATFORM_NS" rollout status "deployment/$deploy" --timeout=600s
done
run "orchestrator-worker" kubectl -n "$PLATFORM_NS" rollout status deployment/orchestrator-worker --timeout=300s
run "console" kubectl -n "$PLATFORM_NS" rollout status deployment/console --timeout=300s
note "temporal, the orchestrator worker and the console ready"

# -- 8. verify -----------------------------------------------------------------------
step "verify"
# Assert every platform pod actually runs this build. Without it, a template
# that forgets the tag value puts stale code back in the cluster and the
# only symptom is a test failing for a bug you already fixed. Company pods
# are the applied blueprint's, not this script's: the acceptance suite
# asserts the same for them once the company has converged.
# Pods on their way out (a superseded ReplicaSet still draining) are not
# "running stale code" — only live ones count. Fields are pipe-delimited so
# an absent deletionTimestamp stays an empty column rather than shifting
# the row.
stale=$(
  kubectl -n "$PLATFORM_NS" get pods \
    -o jsonpath='{range .items[*]}{.status.phase}|{.metadata.deletionTimestamp}|{.metadata.namespace}/{.metadata.name}|{range .spec.containers[*]}{.image} {end}{"\n"}{end}' \
    | awk -F'|' -v tag=":$BUILD_TAG" '
        $2 != "" { next }   # terminating pods
        $4 ~ /poseidon-[a-z-]*:/ && index($4, tag) == 0 { print $3, $4 }
      '
)
[ -z "$stale" ] || fail "pods not running build $BUILD_TAG:
$stale"
note "every platform pod runs $BUILD_TAG"
# No unauthenticated gateway route exists; 401 proves the server is up.
MCP_GATEWAY_URL=${MCP_GATEWAY_URL:-http://127.0.0.1:8080}
code=$(curl -s -o /dev/null -w '%{http_code}' "$MCP_GATEWAY_URL/mcp")
[ "$code" = "401" ] || fail "gateway answered $code, expected 401 without a token"
# The console: a person is refused without a token and known with one. The
# token is the cluster's own — minted with the kubeconfig that just
# installed everything, as any operator would — and saved where every
# later `poseidon` command reads it: a week on kind, for a token good at
# the console and nowhere else. The person is left with one command to
# type, and it is theirs: applying a company.
export POSEIDON_CONSOLE_URL="${POSEIDON_CONSOLE_URL:-http://127.0.0.1:8090}"
for _ in $(seq 60); do
  curl -sf -o /dev/null "$POSEIDON_CONSOLE_URL/healthz" && break
  sleep 1
done
code=$(curl -s -o /dev/null -w '%{http_code}' "$POSEIDON_CONSOLE_URL/whoami")
[ "$code" = "401" ] || fail "console answered $code, expected 401 without a token"
unset POSEIDON_TOKEN
run "login $KIND_HUMAN" uv run poseidon login "$KIND_HUMAN" --namespace "$PLATFORM_NS" --duration 604800 --save
uv run poseidon whoami 2>>"$LOG" | grep -q "\"human\": \"$KIND_HUMAN\"" \
  || fail "the console did not recognise the token of $KIND_HUMAN"
note "gateway and console refuse without a token and know $KIND_HUMAN with one"
close_step
STEP=0  # the summary is not a step; nothing to close on exit

echo "==> platform up in $((SECONDS - STARTED))s in $PLATFORM_NS"
echo "    console $POSEIDON_CONSOLE_URL, gateway $MCP_GATEWAY_URL"
echo "    you are $KIND_HUMAN for a week: \`uv run poseidon whoami\`"
if [ -n "$existing" ]; then
  echo "    the cluster hosts $existing; \`uv run poseidon company show\`"
else
  echo "    next: \`uv run poseidon blueprint apply <blueprint>\` (examples/hello-saas.yaml to try the example)"
fi
echo "    log: $LOG"
