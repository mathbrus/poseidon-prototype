#!/usr/bin/env bash
# Tear down the Poseidon kind cluster. Sandboxes are in-cluster Jobs (plan
# step 4), so deleting the cluster is the whole teardown.
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-poseidon}

kind delete cluster --name "$CLUSTER_NAME"
