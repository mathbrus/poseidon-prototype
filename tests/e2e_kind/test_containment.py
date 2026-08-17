"""Containment on the cluster, adapted to NetworkPolicy + the egress
proxy: from inside the agent pod only the two gateways answer; no pod in the
company namespace carries a credential-shaped env var. The sandbox half —
egress only through the audited proxy, silent NetworkPolicy backstop — is
proven by test_egress.py against a live sandbox.
"""

import subprocess

import pytest

from .conftest import COMPANY_NS, PLATFORM_NS

# Any HTTP answer (401 included) counts as reachable; "blocked" must fail at
# the transport layer within the timeout.
_PROBE = """
import sys, urllib.request, urllib.error
url, want = sys.argv[1], sys.argv[2]
try:
    urllib.request.urlopen(url, timeout=3)
    reachable = True
except urllib.error.HTTPError:
    reachable = True
except Exception:
    reachable = False
sys.exit(0 if reachable == (want == "reachable") else 1)
"""


def _agent_pod() -> str:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            COMPANY_NS,
            "get",
            "pod",
            "-l",
            "poseidon.dev/kind=standing-agent",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def _probe(pod: str, url: str, want: str) -> bool:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            COMPANY_NS,
            "exec",
            pod,
            "--",
            "python",
            "-c",
            _PROBE,
            url,
            want,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode == 0


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
    assert _probe(_agent_pod(), url, want), f"{url} should be {want}"


def test_no_credential_shaped_env_in_company_pods(stack: None) -> None:
    """Same rule as the CI image gate, with no exceptions: nothing
    *_TOKEN/*_KEY in any company pod (every agent's credential — standing
    or crew — is a projected token file, not an env var); GPG_KEY only if
    blanked (the chart empties the python base image's baked-in value)."""
    listing = subprocess.run(
        [
            "kubectl",
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
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    assert listing
    for line in listing:
        pod, _, kind = line.partition("|")
        env = subprocess.run(
            ["kubectl", "-n", COMPANY_NS, "exec", pod, "--", "printenv"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if env.returncode != 0:  # pod terminated mid-scan (sandbox teardown)
            continue
        leaked = [
            line.split("=", 1)[0]
            for line in env.stdout.splitlines()
            if "=" in line
            for name in [line.split("=", 1)[0]]
            if (name.endswith("_TOKEN") or name.endswith("_KEY")) and line != "GPG_KEY="
        ]
        assert not leaked, f"credential-shaped env vars in {pod}: {leaked}"
