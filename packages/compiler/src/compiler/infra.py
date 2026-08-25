"""The infrastructure policy IR and its projections.

One IR compiled from blueprint `stack` + policies; several emissions.
Hand-authoring any projection is a bug — they all come from here.
"""

from typing import Any

from compiler.plan import InfraIR, NetworkPolicyProjection, SandboxConstraints

DEFAULT_IMAGE = "poseidon-sandbox"
DEFAULT_WORKSPACE = "/workspace"
# Resource shape for one coding sandbox: the caps exist and come from one
# place; the blueprint does not tune them per stack.
DEFAULT_CPU = "1"
DEFAULT_MEMORY = "1Gi"
DEFAULT_DISK = "2Gi"
DEFAULT_DEADLINE_S = 3600


def build_infra(blueprint: dict[str, Any], namespace: str) -> InfraIR:
    stack = blueprint.get("stack") or {}
    toolchain = stack.get("toolchain") or {}
    # An empty allowlist means zero egress — the right default for a stack
    # that needs nothing.
    egress_allow = tuple(sorted(toolchain.get("egress_allow") or []))

    sandbox = SandboxConstraints(
        image=toolchain.get("image") or DEFAULT_IMAGE,
        workspace_dir=DEFAULT_WORKSPACE,
        cpu=toolchain.get("cpu") or DEFAULT_CPU,
        memory=toolchain.get("memory") or DEFAULT_MEMORY,
        disk=toolchain.get("disk") or DEFAULT_DISK,
        deadline_s=int(toolchain.get("deadline_s") or DEFAULT_DEADLINE_S),
        egress_allow=egress_allow,
    )
    network_policy = NetworkPolicyProjection(
        namespace=namespace,
        # The sandbox answers the gateway and nobody else; it reaches the
        # audited proxy and nothing else. NetworkPolicy stays the silent
        # backstop beneath the proxy's audited denies.
        #
        # Cluster DNS is deliberately absent: a resolver that forwards
        # unknown names upstream is an exfiltration channel the audited
        # proxy cannot see. The sandbox needs no resolver — with an HTTP
        # proxy the proxy resolves target hosts, and the proxy's own
        # address is resolved by the orchestrator at provision time
        # (sandbox.egressproxy.credentials.resolve_host).
        sandbox_ingress_from=("mcp-gateway",),
        sandbox_egress_to=("egress-proxy",),
    )
    return InfraIR(
        sandbox=sandbox,
        network_policy=network_policy,
        # The proxy enforces exactly what the sandbox is permitted to reach:
        # one list, two consumers, no drift.
        egress_proxy_allowlist=egress_allow,
    )
