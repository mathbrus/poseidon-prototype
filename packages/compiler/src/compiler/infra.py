"""The infrastructure policy IR and its projections.

One IR compiled from blueprint `stack` + policies; several emissions.
Hand-authoring any projection is a bug — they all come from here.
"""

from typing import Any

from compiler.plan import InfraIR, NetworkPolicyProjection, SandboxConstraints

DEFAULT_IMAGE = "poseidon-sandbox"
DEFAULT_WORKSPACE = "/workspace"
# Equal to the sandbox tool server's own default, so a blueprint that
# names no command runs what the image would have run anyway.
DEFAULT_TEST_COMMAND = "pytest -q"
# Resource shape for one coding sandbox. Fixed caps from one place — the
# blueprint's toolchain section names an image, a test command and an
# egress allowlist, never per-stack sizing.
CPU = "1"
MEMORY = "1Gi"
DISK = "2Gi"
DEADLINE_S = 3600


def build_infra(blueprint: dict[str, Any], namespace: str) -> InfraIR:
    stack = blueprint.get("stack") or {}
    toolchain = stack.get("toolchain") or {}
    # An empty allowlist means zero egress — the right default for a stack
    # that needs nothing.
    egress_allow = tuple(sorted(toolchain.get("egress_allow") or []))

    sandbox = SandboxConstraints(
        image=toolchain.get("image") or DEFAULT_IMAGE,
        workspace_dir=DEFAULT_WORKSPACE,
        cpu=CPU,
        memory=MEMORY,
        disk=DISK,
        deadline_s=DEADLINE_S,
        test_command=toolchain.get("test_command") or DEFAULT_TEST_COMMAND,
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
        # The proxy enforces exactly what the sandbox is permitted to reach;
        # the NetworkPolicy beneath it lets the sandbox reach the proxy only.
        egress_proxy_allowlist=egress_allow,
    )
