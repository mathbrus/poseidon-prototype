"""The shape every agent pod shares, standing or ephemeral: what it may
consume and how its readiness is probed. The operator builds standing
Deployments and the orchestrator builds ephemeral pods from the same two
values, so a change here rolls both the same way.
"""

from common.topology import AGENT_PORT

RESOURCES = {
    "requests": {"cpu": "100m", "memory": "256Mi"},
    "limits": {"cpu": "1", "memory": "1Gi"},
}

# The agent host serves its OpenAPI document as soon as it can take a
# dispatch; nothing lighter proves readiness.
READINESS_PROBE = {
    "httpGet": {"path": "/openapi.json", "port": AGENT_PORT},
    "periodSeconds": 3,
    "initialDelaySeconds": 2,
}
