#!/usr/bin/env bash
# The image matrix: every Poseidon image, built from the one Dockerfile at
# the repo root. kind-up.sh and the CI image job both build from here, so
# "which images exist" has one answer.
#
#   deploy/build-images.sh TAG [NAME...]   build every image (or only the named
#                                          ones) as NAME:TAG; prints each reference
#   deploy/build-images.sh --list          print the image names
set -euo pipefail
cd "$(dirname "$0")/.."

# name | workspace package | source packages installed | extra | apt packages | workspace dir | entrypoint
MATRIX='
poseidon-mcp-gateway|mcp-gateway|common registry mcp-gateway||git||uvicorn --factory mcp_gateway.app:create_app --host 0.0.0.0 --port 8080
poseidon-llm-gateway|llm-gateway|common registry llm-gateway||||python -m llm_gateway
poseidon-agent|agent|common schema agent||||uvicorn --factory agent.app:create_app --host 0.0.0.0 --port 8000
poseidon-orchestrator|orchestrator|common schema compiler registry sandbox orchestrator||||python -m orchestrator.worker
poseidon-operator|poseidon-operator|common registry operator||||kopf run -m poseidon_operator.reconcile --all-namespaces
poseidon-sandbox|sandbox|common sandbox|toolchain|git|/workspace|uvicorn sandbox.toolserver.app:app --host 0.0.0.0 --port 8000
poseidon-egress-proxy|sandbox|common registry sandbox|egress|||python -m sandbox.egressproxy
poseidon-mockllm|mockllm|mockllm||||python -m mockllm
'

if [ "${1:-}" = "--list" ]; then
  printf '%s\n' "$MATRIX" | sed '/^$/d' | cut -d'|' -f1
  exit 0
fi

TAG=${1:?usage: build-images.sh TAG [NAME...] | --list}
shift
wanted=("$@")

while IFS='|' read -r name package src extra apt workspace entrypoint; do
  [ -z "$name" ] && continue
  if [ ${#wanted[@]} -gt 0 ] && ! printf '%s\n' "${wanted[@]}" | grep -qx "$name"; then
    continue
  fi
  docker build --quiet -t "$name:$TAG" \
    --build-arg PACKAGE="$package" \
    --build-arg SRC="$src" \
    --build-arg EXTRA="$extra" \
    --build-arg APT="$apt" \
    --build-arg WORKSPACE="$workspace" \
    --build-arg ENTRYPOINT="$entrypoint" \
    . >/dev/null
  echo "$name:$TAG"
done <<<"$MATRIX"
