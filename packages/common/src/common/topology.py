"""Where the platform's pieces are, spelled once.

Every component that builds a pod, an address or a token for another
component imports these — the operator for standing agents, the
orchestrator for ephemeral agents and sandboxes, the gateways to verify
the tokens those pods present. The Helm chart carries the same numbers
as literals (templates cannot import Python); CI asserts they agree.
"""

# Listen ports. The agent host and the sandbox tool server share a number
# because they never share a pod; each is the only server in its own.
AGENT_PORT = 8000
TOOLSERVER_PORT = 8000
MCP_GATEWAY_PORT = 8080
LLM_GATEWAY_PORT = 4000
OPA_PORT = 8181
EGRESS_PROXY_PORT = 3128
BUNDLE_PORT = 8081
CONSOLE_PORT = 8090

# Platform Service names, as the chart names them.
MCP_GATEWAY = "mcp-gateway"
LLM_GATEWAY = "llm-gateway"
EGRESS_PROXY = "egress-proxy"
OPA = "opa"
BUNDLE_SERVER = "bundle-server"
CONSOLE = "console"

# The ports company-namespace NetworkPolicies open toward platform peers.
PEER_PORTS = {
    MCP_GATEWAY: MCP_GATEWAY_PORT,
    LLM_GATEWAY: LLM_GATEWAY_PORT,
    EGRESS_PROXY: EGRESS_PROXY_PORT,
}

# The ServiceAccount the orchestrator worker acts as — the one caller the
# gateways treat as the platform identity.
ORCHESTRATOR_SERVICE_ACCOUNT = "orchestrator-worker"

# The namespace the platform is installed into: the chart's release
# namespace, which every template hands its pod. In-cluster nothing needs
# the literal — but the installer, the console's CLI (which mints a
# human's token from the ServiceAccount the chart declared there) and the
# acceptance harness must agree on it, so it is spelled once.
PLATFORM_NAMESPACE = "poseidon-platform"

# The namespace the company lives in: its standing agents, ephemeral
# agents and sandbox Jobs. A platform hosts one company, so the name is
# fixed and carries no company name — the chart's NetworkPolicies select
# it by this literal, the compiler writes it into the Company CR, and the
# worker puts sandboxes and ephemeral pods here.
COMPANY_NAMESPACE = "poseidon-company"

# Workload identity: every agent pod and the worker present a kubelet-
# rotated projected ServiceAccount token scoped to this audience; both
# gateways verify against it. The token is mounted read-only at this path.
TOKEN_AUDIENCE = "poseidon-mcp-gateway"
TOKEN_MOUNT_DIR = "/var/run/poseidon/identity"
TOKEN_FILE = f"{TOKEN_MOUNT_DIR}/token"
TOKEN_TTL_S = 3600

# Human identity: a person presents a token the API server issued for the
# console's own audience, so a workload's token is worthless at the console
# and a human's token is worthless at either gateway. On a cluster without
# an identity provider, the chart declares each human as a ServiceAccount
# named with this prefix in the platform namespace; a token for it is
# minted through the TokenRequest API with the caller's own kubeconfig.
CONSOLE_TOKEN_AUDIENCE = "poseidon-console"
HUMAN_SA_PREFIX = "human-"

# Labels the operator, the orchestrator and the NetworkPolicies key on.
KIND_LABEL = "poseidon.dev/kind"
RUN_ID_LABEL = "poseidon.dev/run-id"
AGENT_ID_LABEL = "poseidon.dev/agent-id"
COMPANY_LABEL = "poseidon.dev/company"
STANDING_KIND = "standing-agent"
EPHEMERAL_KIND = "ephemeral-agent"
SANDBOX_APP_LABEL = "poseidon-sandbox"

# The platform's own sandbox toolchain, as a blueprint names it. Only the
# deployment knows the content-addressed image reference the cluster
# carries, so the worker maps this logical name to its SANDBOX_IMAGE; any
# other image a blueprint names is used as written.
SANDBOX_TOOLCHAIN = "poseidon-sandbox"


# The header every model call carries: the run the LLM gateway attributes
# the spend to. Compared case-insensitively on the wire.
RUN_ID_HEADER = "X-Poseidon-Run-Id"


def service_url(service: str, namespace: str, port: int) -> str:
    return f"http://{service}.{namespace}.svc.cluster.local:{port}"


def mcp_gateway_url(platform_namespace: str) -> str:
    return service_url(MCP_GATEWAY, platform_namespace, MCP_GATEWAY_PORT)


def llm_gateway_url(platform_namespace: str) -> str:
    return service_url(LLM_GATEWAY, platform_namespace, LLM_GATEWAY_PORT)


def human_service_account(name: str) -> str:
    """The ServiceAccount the chart declares for a human, by their name."""
    return f"{HUMAN_SA_PREFIX}{name}"


def agent_url(standing_name: str, company_namespace: str) -> str:
    """A standing agent's dispatch address: the operator names its Service
    after the hire in the company namespace, so the address is derived,
    never configured."""
    return service_url(standing_name, company_namespace, AGENT_PORT)
