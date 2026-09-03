"""The Kubernetes objects the operator owns, as pure builders.

Pure on purpose: what the operator *would* apply is testable without a
cluster, and the reconcilers below stay thin enough to read. The operator
owns standing structure only — namespaces, NetworkPolicies, standing agent
Deployments — and never creates ephemeral agents or runs processes.
"""

from typing import Any

from common.agentpod import READINESS_PROBE, RESOURCES
from common.podsecurity import CONTAINER_SECURITY_CONTEXT, POD_SECURITY_CONTEXT
from common.topology import (
    AGENT_ID_LABEL,
    AGENT_PORT,
    COMPANY_LABEL,
    EPHEMERAL_KIND,
    KIND_LABEL,
    ORCHESTRATOR_SERVICE_ACCOUNT,
    PEER_PORTS,
    SANDBOX_APP_LABEL,
    STANDING_KIND,
    TOKEN_AUDIENCE,
    TOKEN_FILE,
    TOKEN_MOUNT_DIR,
    TOKEN_TTL_S,
    llm_gateway_url,
    mcp_gateway_url,
)
from poseidon_operator.config import OperatorSettings

LABELS = {
    "app.kubernetes.io/part-of": "poseidon",
    "app.kubernetes.io/managed-by": "poseidon-operator",
}


def namespace(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "labels": {**LABELS, COMPANY_LABEL: name},
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
    """Standing agents and ephemeral crew brains share the agent envelope
    (gateways-only egress, orchestrator-only ingress) — selected by kind,
    so an ephemeral pod is contained the moment it exists."""
    return {
        "matchExpressions": [
            {
                "key": KIND_LABEL,
                "operator": "In",
                "values": [STANDING_KIND, EPHEMERAL_KIND],
            }
        ]
    }


def agent_egress(company_ns: str, platform_ns: str) -> dict[str, Any]:
    """Agents reach the two gateways and cluster DNS. Nothing else
    — an agent with a route to the internet is an agent that can exfiltrate
    without passing a policy check. The model route is the metered LLM
    gateway; the llm-proxy behind it is sealed to everyone but that
    gateway."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "agent-egress", "namespace": company_ns, "labels": LABELS},
        "spec": {
            "podSelector": _agent_kinds_selector(),
            "policyTypes": ["Egress"],
            "egress": [
                _egress_peer(name, platform_ns)
                for name in ("mcp-gateway", "llm-gateway", "kube-dns")
            ],
        },
    }


def agent_ingress(company_ns: str, platform_ns: str) -> dict[str, Any]:
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
                                    "kubernetes.io/metadata.name": platform_ns
                                }
                            },
                            "podSelector": {
                                "matchLabels": {"app": ORCHESTRATOR_SERVICE_ACCOUNT}
                            },
                        }
                    ],
                    "ports": [{"port": AGENT_PORT}],
                }
            ],
        },
    }


def _peer_selector(name: str, platform_ns: str) -> dict[str, Any]:
    """The pods one IR peer name denotes: a platform service by app label,
    or cluster DNS by its kube-system label."""
    if name == "kube-dns":
        return {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
            },
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }
    if name not in PEER_PORTS:
        raise ValueError(f"unknown network peer {name!r}")
    return {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": platform_ns}
        },
        "podSelector": {"matchLabels": {"app": name}},
    }


def _egress_peer(name: str, platform_ns: str) -> dict[str, Any]:
    """The egress rule that lets a pod dial `name` — on the port that peer
    listens on."""
    ports = (
        [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]
        if name == "kube-dns"
        else [{"port": PEER_PORTS[name]}]
    )
    return {"to": [_peer_selector(name, platform_ns)], "ports": ports}


def _ingress_peer(name: str, platform_ns: str) -> dict[str, Any]:
    """The ingress rule that lets `name` dial a pod — on the port our pods
    listen on."""
    return {"from": [_peer_selector(name, platform_ns)], "ports": [{"port": AGENT_PORT}]}


def sandbox_isolation(
    company_ns: str, platform_ns: str, projection: dict[str, Any] | None = None
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
    ingress = [_ingress_peer(name, platform_ns) for name in ingress_from]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "sandbox-isolation",
            "namespace": company_ns,
            "labels": LABELS,
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": SANDBOX_APP_LABEL}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress,
            "egress": [_egress_peer(name, platform_ns) for name in egress_to],
        },
    }


def sandbox_provisioner_role(company_ns: str) -> dict[str, Any]:
    """The orchestrator worker's Kubernetes surface — deliberately bounded,
    and a review artifact: it is part of the trusted computing base. Two
    consumers: the k8s_jobs sandbox backend (Jobs + pod reads + exec for
    workspace delivery) and the ephemeral-pod provisioner (pod + ServiceAccount
    create/delete for each ephemeral agent — the SA is the
    agent's per-run identity, so its lifetime must equal the pod's, which
    is why it is provisioned here and not operator-reconciled). Company
    namespaces only — the Role is namespaced and operator-reconciled, so
    the worker can create no pod or SA in the platform namespace; the
    run_id+kind label-selector discipline on deletes lives in
    `orchestrator.ephemeral_pods` (RBAC cannot express selectors), proven by the
    ephemeral-pods e2e. Never Deployments, Services or CRs: standing structure stays
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
                # create/delete exist for ephemeral pods.
                "verbs": ["create", "get", "list", "delete"],
            },
            {
                "apiGroups": [""],
                "resources": ["serviceaccounts"],
                # Per-run crew identities: one SA per ephemeral agent per run,
                # created at provision, deleted by the teardown sweep.
                # Deliberately no RoleBindings here — an SA the worker can
                # create binds to nothing, so this grants identity, never
                # authority.
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


def sandbox_provisioner_binding(company_ns: str, platform_ns: str) -> dict[str, Any]:
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
                "namespace": platform_ns,
            }
        ],
    }


def agent_service_account(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    """The standing agent's own identity. The name is the
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
            "labels": {**LABELS, COMPANY_LABEL: spec["company"]},
        },
    }


def agent_deployment(
    spec: dict[str, Any], company_ns: str, settings: OperatorSettings
) -> dict[str, Any]:
    """A standing agent Deployment, reconciled from an AgentInstance.

    The spec carries no credential: the pod's only identity is the
    audience-scoped projected token the kubelet mounts and rotates.
    No API-server token is mounted (automount stays off), and no
    token-shaped env var exists in the pod at all.
    """
    standing_name = spec["standingName"]
    platform_ns = settings.platform_namespace
    labels = {
        **LABELS,
        "app": standing_name,
        KIND_LABEL: STANDING_KIND,
        COMPANY_LABEL: spec["company"],
        AGENT_ID_LABEL: spec["agentId"],
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": standing_name, "namespace": company_ns, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": standing_name}},
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
                    # stays writable — the OpenHands SDK writes under $HOME.
                    "securityContext": dict(POD_SECURITY_CONTEXT),
                    "volumes": [
                        {
                            "name": "gateway-identity",
                            "projected": {
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "audience": TOKEN_AUDIENCE,
                                            "expirationSeconds": TOKEN_TTL_S,
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
                            "image": settings.agent_image,
                            "imagePullPolicy": "IfNotPresent",
                            "securityContext": dict(CONTAINER_SECURITY_CONTEXT),
                            "ports": [{"containerPort": AGENT_PORT}],
                            "env": [
                                {
                                    "name": "MCP_GATEWAY_URL",
                                    "value": mcp_gateway_url(platform_ns),
                                },
                                {
                                    "name": "LLM_PROXY_URL",
                                    "value": llm_gateway_url(platform_ns),
                                },
                                # Everything the agent reads about itself and
                                # its company rides the template, so an edit
                                # to any of it converges as a rollout; tools
                                # are policed at the gateway, never handed to
                                # the pod. MODEL is cluster config: the LLM
                                # gateway pins the same model on every call.
                                {"name": "ROLE", "value": spec["role"]},
                                {
                                    "name": "MODEL",
                                    "value": settings.model,
                                },
                                {
                                    "name": "TARGET_REPO",
                                    "value": spec["repo"]["url"],
                                },
                                {"name": "MISSION", "value": spec["mission"]},
                                {
                                    "name": "GATEWAY_TOKEN_FILE",
                                    "value": TOKEN_FILE,
                                },
                                # The python base image bakes this in; blank
                                # it so no token-shaped value exists in a
                                # company pod at all.
                                {"name": "GPG_KEY", "value": ""},
                            ],
                            "volumeMounts": [
                                {
                                    "name": "gateway-identity",
                                    "mountPath": TOKEN_MOUNT_DIR,
                                    "readOnly": True,
                                }
                            ],
                            "resources": dict(RESOURCES),
                            "readinessProbe": dict(READINESS_PROBE),
                        }
                    ],
                },
            },
        },
    }


def agent_service(spec: dict[str, Any], company_ns: str) -> dict[str, Any]:
    standing_name = spec["standingName"]
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": standing_name, "namespace": company_ns, "labels": LABELS},
        "spec": {
            "selector": {"app": standing_name},
            "ports": [{"port": AGENT_PORT, "targetPort": AGENT_PORT}],
        },
    }
