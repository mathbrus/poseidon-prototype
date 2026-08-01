#!/usr/bin/env bash
# Tear down the Poseidon kind cluster and the transitional sandbox network.
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-poseidon}
SANDBOX_NETWORK=${SANDBOX_NETWORK:-poseidon-kind-sandboxnet}

kind delete cluster --name "$CLUSTER_NAME"

if docker network inspect "$SANDBOX_NETWORK" >/dev/null 2>&1; then
  # Any leaked sandbox containers must go before the network can.
  for container in $(docker network inspect "$SANDBOX_NETWORK" \
      --format '{{range .Containers}}{{.Name}} {{end}}'); do
    docker rm -f "$container"
  done
  docker network rm "$SANDBOX_NETWORK"
fi
