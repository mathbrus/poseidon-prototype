"""The Kubernetes objects the operator owns, as pure builders.

Pure on purpose: what the operator *would* apply is testable without a
cluster, and the reconcilers below stay thin enough to read. The operator
owns standing structure only — namespaces, NetworkPolicies, standing agent
Deployments — and never creates ephemeral agents or runs processes.
"""

import os
from typing import Any

from common.podsecurity import CONTAINER_SECURITY_CONTEXT, POD_SECURITY_CONTEXT

AGENT_PORT = 8000
LABELS = {
    "app.kubernetes.io/part-of": "poseidon",
    "app.kubernetes.io/managed-by": "poseidon-operator",
}
# Workload identity: every standing agent runs as its own
# ServiceAccount, named by its agent_id, and authenticates to the MCP
# gateway with a kubelet-rotated projected token scoped to this audience.
# Must match the gateway's `token_audience` setting.
TOKEN_AUDIENCE = "poseidon-gateway"
TOKEN_MOUNT_DIR = "/var/run/poseidon/identity"


def platform_namespace() -> str:
    return os.environ.get("PLATFORM_NAMESPACE", "poseidon-platform")


def namespace(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {**LABELS, "poseidon.dev/company": name},
        },
    }


def default_deny(company_ns: str) -> dict[str, Any]:
    """Deny-all is the envelope every other rule punches through."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": company_ns, "labels": LABELS},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }


def _agent_kinds_selector() -> dict[str, Any]:
    """Standing hires and ephemeral crew brains share the agent envelope
    (gateways-only egress, orchestrator-only ingress) — selected by kind,
    so a crew pod (step 16c) is contained the moment it exists."""
    return {
        "matchExpressions": [
            {
                "key": "poseidon.dev/kind",
                "operator": "In",
                "values": ["standing-agent", "crew-agent"],
            }
        ]
    }


def agent_egress(company_ns: str) -> dict[str, Any]:
    """Agents reach the two gateways and cluster DNS. Nothing else
    — an agent with a route to the internet is an agent that can exfiltrate
    without passing a policy check."""
    platform = platform_namespace()
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-egress", "namespace": company_ns, "labels": LABELS},
        "spec": {
            "podSelector": _agent_kinds_selector(),
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": platform}
                            },
                            "podSelector": {"matchLabels": {"app": "mcp-gateway"}},
                        }
                    ],
                    "ports": [{"port": 8080}],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": platform}
                            },
                            # The metered LLM gateway — the
                            # llm-proxy behind it is sealed to everyone
                            # but that gateway.
                            "podSelector": {"matchLabels": {"app": "llm-gateway"}},
                        }
                    ],
                    "ports": [{"port": 4000}],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"port": 53, "protocol": "UDP"},
                        {"port": 53, "protocol": "TCP"},
                    ],
                },
            ],
        },
    }


def agent_ingress(company_ns: str) -> dict[str, Any]:
    """Only the orchestrator dispatches into an agent — standing or crew."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-ingress", "namespace": company_ns, "labels": LABELS},
        "spec": {
            "podSelector": _agent_kinds_selector(),
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": platform_namespace()
                                }
                            },
                            "podSelector": {
                                "matchLabels": {"app": "orchestrator-worker"}
                            },
                        }
                    ],
                    "ports": [{"port": AGENT_PORT}],
                }
            ],
        },
    }


def _peer_rule(name: str) -> dict[str, Any]:
    """One IR peer name -> the NetworkPolicy rule that admits exactly it.
    The vocabulary is the infra IR projection's: platform services by app label,
    cluster DNS by its kube-system label."""
    if name == "kube-dns":
        return {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        }
    ports = {"mcp-gateway": AGENT_PORT, "egress-proxy": 3128}
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": platform_namespace()}
                },
                "podSelector": {"matchLabels": {"app": name}},
            }
        ],
        "ports": [{"port": ports.get(name, AGENT_PORT)}],
    }


def sandbox_isolation(
    company_ns: str, projection: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The sandbox envelope, projected from the compiled infra IR: answer
    the gateway, reach only the audited proxy. NetworkPolicy is the silent
    backstop beneath the proxy's audited denies. The defaults equal what
    the compiler emits for every blueprint today, so a CR carrying no
    projection converges identically.

    No cluster DNS: a resolver forwarding unknown names upstream is a
    channel the audited proxy cannot see, and the sandbox needs no
    resolver (the proxy resolves target hosts; the orchestrator resolves
    the proxy's own address at provision time)."""
    projection = projection or {}
    ingress_from = list(projection.get("sandbox_ingress_from") or ["mcp-gateway"])
    egress_to = list(projection.get("sandbox_egress_to") or ["egress-proxy"])
    ingress = [
        {"from": rule["to"], "ports": rule["ports"]}
        for rule in (_peer_rule(name) for name in ingress_from)
    ]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "sandbox-isolation",
            "namespace": company_ns,
            "labels": LABELS,
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": "poseidon-sandbox"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress,
            "egress": [_peer_rule(name) for name in egress_to],
        },
    }


def sandbox_provisioner_role(company_ns: str) -> dict[str, Any]:
    """The orchestrator worker's Kubernetes surface — deliberately bounded,
    and a review artifact: it is part of the trusted computing base. Two
    consumers: the k8s_jobs sandbox backend (Jobs + pod reads + exec for
    workspace delivery) and the crew-pod provisioner (pod
    create/delete for the ephemeral executor's brain host). Company
    namespaces only — the Role is namespaced and operator-reconciled, so
    the worker can create no pod in the platform namespace; the
    run_id+kind label-selector discipline on deletes lives in
    `orchestrator.crewpods` (RBAC cannot express selectors), proven by the
    16c e2e. Never Deployments, Services or CRs: standing structure stays
    operator-owned."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": "sandbox-provisioner",
            "namespace": company_ns,
            "labels": LABELS,
        },
        "rules": [
            {
                "apiGroups": ["batch"],
                "resources": ["jobs"],
                "verbs": ["create", "get", "list", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods"],
                # create/delete joined at step 16c for crew pods.
                "verbs": ["create", "get", "list", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["pods/exec"],
                # Both verbs are needed: kubectl exec POSTs (create), but the
                # Python client's exec is a WebSocket GET handshake (get).
                "verbs": ["create", "get"],
            },
        ],
    }


def sandbox_provisioner_binding(company_ns: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "sandbox-provisioner",
            "namespace": company_ns,
            "labels": LABELS,
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "sandbox-provisioner",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "orchestrator-worker",
                "namespace": platform_namespace(),
            }
        ],
    }


def agent_service_account(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    """The instance's own identity. The name is the
    agent_id: the gateway resolves `system:serviceaccount:<ns>:<agent_id>`
    against the registry's `agent_instances` row and requires the namespace
    to match — a same-named ServiceAccount anywhere else verifies fine at
    the API server and still maps to nothing."""
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": spec["agentId"],
            "namespace": company_ns,
            "labels": {**LABELS, "poseidon.dev/company": spec["company"]},
        },
    }


def agent_deployment(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    """A standing agent Deployment, reconciled from an AgentInstance.

    The spec carries no credential: the pod's only identity is the
    audience-scoped projected token the kubelet mounts and rotates.
    No API-server token is mounted (automount stays off), and no
    token-shaped env var exists in the pod at all (E3).
    """
    hire = spec["hire"]
    env = os.environ
    labels = {
        **LABELS,
        "app": hire,
        "poseidon.dev/kind": "standing-agent",
        "poseidon.dev/company": spec["company"],
        "poseidon.dev/agent-id": spec["agentId"],
        # In the pod-template labels so a blueprint edit to the tier rolls
        # the pod — the step-10 done-when — and so it is observable.
        "poseidon.dev/model-tier": spec.get("modelTier", ""),
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": hire, "namespace": company_ns, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": hire}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": spec["agentId"],
                    # The projected volume below is the pod's only token:
                    # audience-scoped to the gateway, useless at the API
                    # server. Automount stays off.
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    # Non-root, no privilege-escalation path, no
                    # capabilities, default seccomp filter. Root filesystem
                    # stays writable — the brain SDK writes under $HOME.
                    "securityContext": dict(POD_SECURITY_CONTEXT),
                    "volumes": [
                        {
                            "name": "gateway-identity",
                            "projected": {
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": TOKEN_AUDIENCE,
                                            "expirationSeconds": 3600,
                                            "path": "token",
                                        }
                                    }
                                ]
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "agent",
                            "image": env.get("AGENT_IMAGE", "poseidon-agent:latest"),
                            "imagePullPolicy": "IfNotPresent",
                            "securityContext": dict(CONTAINER_SECURITY_CONTEXT),
                            "ports": [{"containerPort": AGENT_PORT}],
                            "env": [
                                {
                                    "name": "MCP_GATEWAY_URL",
                                    "value": f"http://mcp-gateway.{platform_namespace()}.svc.cluster.local:8080",
                                },
                                {
                                    "name": "LLM_PROXY_URL",
                                    "value": f"http://llm-gateway.{platform_namespace()}.svc.cluster.local:4000",
                                },
                                # The spec's tier and tool scope ride the pod
                                # template so an AgentInstance edit converges
                                # as a rollout. MODEL itself stays cluster
                                # config: the LLM gateway resolves
                                # tier -> model.
                                {
                                    "name": "MODEL_TIER",
                                    "value": spec.get("modelTier", ""),
                                },
                                {"name": "ROLE_TEMPLATE", "value": spec["roleTemplate"]},
                                {
                                    "name": "TOOLS",
                                    "value": ",".join(sorted(spec.get("tools") or [])),
                                },
                                {
                                    "name": "MODEL",
                                    "value": env.get("MODEL", "mock-brain"),
                                },
                                {
                                    "name": "TARGET_REPO",
                                    "value": env.get("TARGET_REPO", ""),
                                },
                                {
                                    "name": "MAX_ITERATIONS",
                                    "value": env.get("MAX_ITERATIONS", "10"),
                                },
                                {
                                    "name": "GATEWAY_TOKEN_FILE",
                                    "value": f"{TOKEN_MOUNT_DIR}/token",
                                },
                                # The python base image bakes this in; blank
                                # it so no token-shaped value exists in a
                                # company pod at all (E3).
                                {"name": "GPG_KEY", "value": ""},
                            ],
                            "volumeMounts": [
                                {
                                    "name": "gateway-identity",
                                    "mountPath": TOKEN_MOUNT_DIR,
                                    "readOnly": True,
                                }
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/openapi.json", "port": AGENT_PORT},
                                "periodSeconds": 5,
                                "initialDelaySeconds": 3,
                            },
                        }
                    ],
                },
            },
        },
    }


def agent_service(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    hire = spec["hire"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": hire, "namespace": company_ns, "labels": LABELS},
        "spec": {
            "selector": {"app": hire},
            "ports": [{"port": AGENT_PORT, "targetPort": AGENT_PORT}],
        },
    }
