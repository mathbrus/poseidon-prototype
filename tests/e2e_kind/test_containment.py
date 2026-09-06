"""Containment on the cluster, adapted to NetworkPolicy + the egress
proxy: from inside the agent pod only the two gateways answer; no pod in the
company namespace carries a credential-shaped env var. The sandbox half —
egress only through the audited proxy, silent NetworkPolicy backstop — is
proven by test_redteam.py against a live sandbox.
"""

import pytest

from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    agent_pod,
    kubectl,
    platform_pod,
    probe,
    run_kubectl,
)

# Temporal's HTTP API port: a real HTTP status answers a reachable probe,
# where the gRPC port would make every reply look like a refusal.
TEMPORAL_HTTP = f"http://temporal-frontend.{PLATFORM_NS}.svc.cluster.local:7243/"


@pytest.mark.parametrize(
    ("url", "want"),
    [
        (f"http://mcp-gateway.{PLATFORM_NS}.svc.cluster.local:8080/mcp", "reachable"),
        (f"http://llm-gateway.{PLATFORM_NS}.svc.cluster.local:4000/healthz", "reachable"),
        # The llm-proxy (provider keys) is sealed behind the metered
        # gateway — an agent must not reach it.
        (
            f"http://llm-proxy.{PLATFORM_NS}.svc.cluster.local:4000/health/liveliness",
            "blocked",
        ),
        (f"http://opa.{PLATFORM_NS}.svc.cluster.local:8181/health", "blocked"),
        (f"http://egress-proxy.{PLATFORM_NS}.svc.cluster.local:3128/healthz", "blocked"),
        ("http://1.1.1.1", "blocked"),
        ("http://api.github.com", "blocked"),
    ],
)
def test_agent_reaches_only_the_gateways(stack: None, url: str, want: str) -> None:
    assert probe(agent_pod(), url, want), f"{url} should be {want}"


@pytest.mark.parametrize(
    ("app", "want"),
    [
        # Reaching the frontend is starting runs and delivering gate
        # signals: the worker hosts the workflows and the console acts on a
        # human's behalf; a platform pod with neither job is refused.
        ("orchestrator-worker", "reachable"),
        ("console", "reachable"),
        ("llm-gateway", "blocked"),
    ],
)
def test_temporal_frontend_is_fenced_from_platform_pods(
    stack: None, app: str, want: str
) -> None:
    assert probe(platform_pod(app), TEMPORAL_HTTP, want, namespace=PLATFORM_NS), (
        f"the frontend should be {want} from {app}"
    )


def test_no_credential_shaped_env_in_company_pods(stack: None) -> None:
    """Same rule as the CI image gate, with no exceptions: nothing
    *_TOKEN/*_KEY in any company pod (every agent's credential — standing
    or crew — is a projected token file, not an env var); GPG_KEY only if
    blanked (the chart empties the python base image's baked-in value)."""
    listing = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "pods",
        "--field-selector=status.phase=Running",
        "-o",
        (
            "jsonpath={range .items[*]}{.metadata.name}"
            "|{.metadata.labels.poseidon\\.dev/kind}{'\\n'}{end}"
        ),
        timeout=30,
    ).splitlines()
    assert listing
    for line in listing:
        pod, _, _kind = line.partition("|")
        env = run_kubectl("-n", COMPANY_NS, "exec", pod, "--", "printenv")
        if env.returncode != 0:  # pod terminated mid-scan (sandbox teardown)
            continue
        leaked = [
            name
            for line in env.stdout.splitlines()
            if "=" in line
            for name in [line.split("=", 1)[0]]
            if (name.endswith("_TOKEN") or name.endswith("_KEY")) and line != "GPG_KEY="
        ]
        assert not leaked, f"credential-shaped env vars in {pod}: {leaked}"
