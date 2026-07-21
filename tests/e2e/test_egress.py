"""E3 — egress: the network topology, not convention, is the enforcement.

From inside the agent container the only reachable endpoints are the MCP
gateway and the LLM proxy; the internet fails at the network level. From
inside a live sandbox container on `sandboxnet`, *all* external egress fails
— including the LLM proxy, which sandboxes must never see.
"""

import subprocess

import pytest

from tests.e2e.conftest import compose_exec

# Fails a socket connect (resolution failure counts: internal networks have
# no route and no DNS for the outside world), exit 0 = unreachable.
CONNECT_SNIPPET = """
import socket, sys
try:
    socket.create_connection(({host!r}, {port}), timeout=5).close()
except OSError:
    sys.exit(0)
sys.exit(1)
"""

# HTTP reachability; any HTTP status (e.g. the gateway's 401) counts as
# reachable — this is a network test, not an auth test.
HTTP_SNIPPET = """
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen({url!r}, timeout=5)
except urllib.error.HTTPError:
    pass
except OSError:
    sys.exit(1)
sys.exit(0)
"""


def in_agent(snippet: str) -> subprocess.CompletedProcess:
    return compose_exec("agent", "python", "-c", snippet)


@pytest.fixture(scope="module")
def sandbox_container(env: dict[str, str]) -> str:
    """A throwaway container from the sandbox image on the sandbox network."""
    image = env.get("SANDBOX_IMAGE", "poseidon-sandbox")
    network = env.get("SANDBOX_NETWORK", "poseidon-sandboxnet")
    name = "poseidon-e2e-egress-probe"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name, "--network", network,
         "--entrypoint", "sleep", image, "600"],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, f"cannot start sandbox probe: {run.stderr}"
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def in_sandbox(container: str, snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, "python", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_agent_cannot_reach_the_internet(stack: None) -> None:
    probe = in_agent(CONNECT_SNIPPET.format(host="github.com", port=443))
    assert probe.returncode == 0, "agent container opened a socket to github.com:443"


def test_agent_reaches_gateway_and_llm_proxy(stack: None) -> None:
    for url in ("http://mcp-gateway:8080/mcp", "http://llm-proxy:4000/health/liveliness"):
        probe = in_agent(HTTP_SNIPPET.format(url=url))
        assert probe.returncode == 0, f"agent cannot reach {url}: {probe.stderr}"


def test_sandbox_has_zero_egress(stack: None, sandbox_container: str) -> None:
    for host, port in (("github.com", 443), ("llm-proxy", 4000)):
        probe = in_sandbox(
            sandbox_container, CONNECT_SNIPPET.format(host=host, port=port)
        )
        assert probe.returncode == 0, f"sandbox opened a socket to {host}:{port}"


def test_no_secret_shaped_variable_in_agent_env(stack: None) -> None:
    listing = compose_exec("agent", "env")
    assert listing.returncode == 0, listing.stderr
    secret_shaped = [
        line
        for line in listing.stdout.splitlines()
        if line.split("=", 1)[0].upper().endswith(("_TOKEN", "_KEY"))
    ]
    assert secret_shaped == [
        line for line in secret_shaped if line.startswith("AGENT_BEARER_TOKEN=")
    ], f"unexpected secret-shaped variables in agent env: {secret_shaped}"
