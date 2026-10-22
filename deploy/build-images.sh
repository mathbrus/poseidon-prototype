#!/usr/bin/env bash
# The image matrix: every Poseidon image, built from the one Dockerfile at
# the repo root. kind-up.sh and the CI image job both build from here, so
# "which images exist" has one answer.
#
#   deploy/build-images.sh TAG [NAME...]   build every image (or only the named
#                                          ones) as NAME:TAG; prints each reference
#   deploy/build-images.sh --list          print the image names
#   deploy/build-images.sh --version       print the version the images are stamped with
set -euo pipefail
cd "$(dirname "$0")/.."

# The version every image answers to (common.version): the tree's
# `git describe` against the release tags — the tag itself on a release,
# the tag plus distance and commit between releases, `-dirty` on an
# uncommitted tree, a bare commit before the first release. Empty outside
# git, which the image reports as unreleased.
version() {
  git describe --tags --match 'v*' --always --dirty 2>/dev/null || true
}

# name | workspace package | source packages installed | extra | apt packages | workspace dir | entrypoint
MATRIX='
poseidon-mcp-gateway|mcp-gateway|common blueprint registry mcp-gateway||git||uvicorn --factory mcp_gateway.app:create_app --host 0.0.0.0 --port 8080
poseidon-llm-gateway|llm-gateway|common registry llm-gateway||||python -m llm_gateway
poseidon-agent-host|agent-host|common blueprint agent-host||||uvicorn --factory agent_host.app:create_app --host 0.0.0.0 --port 8000
poseidon-orchestrator|orchestrator|common blueprint compiler registry sandbox orchestrator||||python -m orchestrator.worker
poseidon-reconciler|poseidon-reconciler|common registry reconciler||||kopf run -m reconciler.reconcile --all-namespaces
poseidon-sandbox|sandbox|common sandbox|toolchain|git ripgrep|/workspace|uvicorn sandbox.toolserver.app:app --host 0.0.0.0 --port 8000
poseidon-egress-proxy|sandbox|common registry sandbox|egress|||python -m sandbox.egressproxy
poseidon-mock-brain|mock-brain|mock-brain||||python -m mock_brain
poseidon-console|console|common blueprint registry compiler console||||uvicorn --factory console.app:create_app --host 0.0.0.0 --port 8090
'

if [ "${1:-}" = "--list" ]; then
  printf '%s\n' "$MATRIX" | sed '/^$/d' | cut -d'|' -f1
  exit 0
fi
if [ "${1:-}" = "--version" ]; then
  version
  exit 0
fi

TAG=${1:?usage: build-images.sh TAG [NAME...] | --list | --version}
shift
wanted=("$@")
VERSION=$(version)

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
    --build-arg VERSION="$VERSION" \
    . >/dev/null
  echo "$name:$TAG"
done <<<"$MATRIX"
