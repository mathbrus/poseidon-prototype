"""The Kubernetes objects the operator owns, as pure builders.

Pure on purpose: what the operator *would* apply is testable without a
cluster, and the reconcilers below stay thin enough to read. The operator
owns standing structure only — namespaces, NetworkPolicies, standing agent
Deployments — and never creates ephemeral agents or runs processes
(§3 ownership matrix).
"""

import os
from typing import Any

AGENT_PORT = 8000
LABELS = {"app.kubernetes.io/part-of": "poseidon", "app.kubernetes.io/managed-by": "poseidon-operator"}


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


def agent_egress(company_ns: str) -> dict[str, Any]:
    """Standing agents reach the two gateways and cluster DNS. Nothing else
    — an agent with a route to the internet is an agent that can exfiltrate
    without passing a policy check."""
    platform = platform_namespace()
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-egress", "namespace": company_ns, "labels": LABELS},
        "spec": {
            "podSelector": {"matchLabels": {"poseidon.dev/kind": "standing-agent"}},
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
                            "podSelector": {"matchLabels": {"app": "llm-proxy"}},
                        }
                    ],
                    "ports": [{"port": 4000}],
                },
                {
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
                },
            ],
        },
    }


def agent_ingress(company_ns: str) -> dict[str, Any]:
    """Only the orchestrator dispatches into a standing agent."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-ingress", "namespace": company_ns, "labels": LABELS},
        "spec": {
            "podSelector": {"matchLabels": {"poseidon.dev/kind": "standing-agent"}},
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
                            "podSelector": {"matchLabels": {"app": "orchestrator-worker"}},
                        }
                    ],
                    "ports": [{"port": AGENT_PORT}],
                }
            ],
        },
    }


def sandbox_isolation(company_ns: str) -> dict[str, Any]:
    """The sandbox envelope, from the infra IR (§5.4): answer the gateway,
    reach only the audited proxy. NetworkPolicy is the silent backstop
    beneath the proxy's audited denies."""
    platform = platform_namespace()
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
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": platform}
                            },
                            "podSelector": {"matchLabels": {"app": "mcp-gateway"}},
                        }
                    ],
                    "ports": [{"port": AGENT_PORT}],
                }
            ],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": platform}
                            },
                            "podSelector": {"matchLabels": {"app": "egress-proxy"}},
                        }
                    ],
                    "ports": [{"port": 3128}],
                },
                {
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
                },
            ],
        },
    }


def sandbox_provisioner_role(company_ns: str) -> dict[str, Any]:
    """The orchestrator worker's Kubernetes surface for the k8s_jobs sandbox
    backend (plan step 4) — deliberately bounded, and a review artifact per
    the overview §3 TCB stance: Jobs and pods in the company namespace only.
    pods/exec exists solely for workspace delivery and provisioning probes.
    Operator-reconciled since step 10, because the namespace it lives in is
    operator-created; step 16c widens this surface for crew pods."""
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
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
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


def agent_deployment(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    """A standing agent Deployment, reconciled from an AgentInstance.

    The spec carries no credential: the agent's bearer token comes from the
    company-namespace secret the operator syncs, and the whole thing is
    replaced by a projected identity token at plan step 11/12.
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
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "containers": [
                        {
                            "name": "agent",
                            "image": env.get("AGENT_IMAGE", "poseidon-agent:latest"),
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": AGENT_PORT}],
                            "env": [
                                {"name": "MCP_GATEWAY_URL",
                                 "value": f"http://mcp-gateway.{platform_namespace()}.svc.cluster.local:8080"},
                                {"name": "LLM_PROXY_URL",
                                 "value": f"http://llm-proxy.{platform_namespace()}.svc.cluster.local:4000"},
                                # The spec's tier and tool scope ride the pod
                                # template so an AgentInstance edit converges
                                # as a rollout. MODEL itself stays cluster
                                # config until the LLM gateway resolves
                                # tier -> model at plan step 14.
                                {"name": "MODEL_TIER",
                                 "value": spec.get("modelTier", "")},
                                {"name": "ROLE_TEMPLATE",
                                 "value": spec["roleTemplate"]},
                                {"name": "TOOLS",
                                 "value": ",".join(sorted(spec.get("tools") or []))},
                                {"name": "MODEL", "value": env.get("MODEL", "mock-brain")},
                                {"name": "TARGET_REPO", "value": env.get("TARGET_REPO", "")},
                                {"name": "MAX_ITERATIONS",
                                 "value": env.get("MAX_ITERATIONS", "10")},
                                {"name": "AGENT_BEARER_TOKEN",
                                 "valueFrom": {"secretKeyRef": {
                                     "name": "agent-bearer-token",
                                     "key": "AGENT_BEARER_TOKEN"}}},
                                # The python base image bakes this in; blank
                                # it so no token-shaped value exists in a
                                # company pod at all (E3).
                                {"name": "GPG_KEY", "value": ""},
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
