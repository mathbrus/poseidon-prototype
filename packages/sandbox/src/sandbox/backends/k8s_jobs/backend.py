"""Kubernetes-Jobs implementation of the SandboxBackend contract.

One Job per run in the company namespace: resource caps and
activeDeadlineSeconds come from the SandboxPolicy, the toolserver image runs
as the only container, and the pod is addressed by IP (the gateway's egress
NetworkPolicy allows it by pod selector). The workspace tarball is delivered
at startup over the Kubernetes exec API — the same API-server path kubectl
exec uses — so it needs no ingress exception: the sandbox NetworkPolicy keeps
ingress gateway-only and egress proxy-only.

Only the orchestrator calls provision/destroy; its ServiceAccount
carries the matching bounded Role (jobs, pods, pods/exec in the company
namespace only — see deploy/helm .../orchestrator-rbac.yaml).
"""

import base64
import time

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from common.telemetry import get_logger
from sandbox.backends.k8s_jobs.manifest import (
    TOOLSERVER_PORT,
    build_job,
    job_name,
)
from sandbox.contract import SandboxHandle, SandboxPolicy

POD_START_TIMEOUT_S = 120
READY_TIMEOUT_S = 60
DELETE_TIMEOUT_S = 120
EXEC_TIMEOUT_S = 120
UPLOAD_CHUNK = 1 << 20

# Probes the tool server from inside the pod (pod IPs are not routable from
# the provisioning host in general, and provision must not return before the
# tool server accepts connections: the agent's first proxied call is not
# retried).
_READY_CMD = (
    "import urllib.request; "
    f"urllib.request.urlopen('http://localhost:{TOOLSERVER_PORT}/openapi.json', timeout=1)"
)

# Runs once after the workspace is unpacked: no remote may survive inside the
# sandbox, the base ref is recorded for later `git bundle` calls, and the
# platform marker plus build artifacts are kept out of deliveries via
# `.git/info/exclude` — git's local ignore, never committed, so `git add -A`
# skips them and the PR diff is exactly the agent's own changes. This is
# what lets a policy-resolved merge gate confine a delegated class to real
# source paths: it refuses any path outside that class, so an incidental
# `.poseidon-base` or `__pycache__` in the diff would park the merge. The
# marker file still exists on disk for the tool server to read — excluding
# it from git does not remove it.
_PREPARE_CMD = (
    "set -e; cd {workspace}; "
    "git remote | xargs -r -n1 git remote remove; "
    "git rev-parse HEAD > .poseidon-base; "
    "printf '.poseidon-base\\n__pycache__/\\n*.pyc\\n' >> .git/info/exclude"
)

_UPLOAD_PATH = "/tmp/poseidon-workspace.b64"


class K8sJobsBackend:
    def __init__(self, namespace: str, api_client: k8s.ApiClient | None = None) -> None:
        if api_client is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            api_client = k8s.ApiClient()
        self._batch = k8s.BatchV1Api(api_client)
        self._core = k8s.CoreV1Api(api_client)
        self._namespace = namespace
        self._log = get_logger("sandbox.backends.k8s_jobs")

    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle:
        name = job_name(run_id)
        # Retry-safe under the durable orchestrator: a crashed earlier
        # attempt may have left a Job with this name (one provision per run,
        # so it can only be our own remnant) — remove it first.
        if self._delete_job(name):
            self._log.info("removed remnant sandbox", extra={"run_id": run_id})
        self._batch.create_namespaced_job(self._namespace, build_job(run_id, policy))
        try:
            pod = self._wait_pod_running(name)
            pod_name = pod.metadata.name
            self._upload_workspace(pod_name, workspace_tar, policy.workspace_dir)
            exit_code, output = self._exec(
                pod_name,
                ["sh", "-c", _PREPARE_CMD.format(workspace=policy.workspace_dir)],
            )
            if exit_code != 0:
                raise RuntimeError(f"workspace preparation failed: {output.strip()}")
            deadline = time.monotonic() + READY_TIMEOUT_S
            while True:
                exit_code, _ = self._exec(pod_name, ["python", "-c", _READY_CMD])
                if exit_code == 0:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"tool server not ready after {READY_TIMEOUT_S}s")
                time.sleep(0.5)
        except Exception:
            self._delete_job(name)
            raise
        handle = SandboxHandle(
            run_id=run_id,
            url=f"http://{pod.status.pod_ip}:{TOOLSERVER_PORT}",
            namespace=self._namespace,
            job_name=name,
            pod_name=pod_name,
        )
        self._log.info(
            "sandbox provisioned",
            extra={"run_id": run_id, "job_name": name, "url": handle.url},
        )
        return handle

    def destroy(self, handle: SandboxHandle) -> None:
        if self._delete_job(handle.job_name or job_name(handle.run_id), wait=True):
            self._log.info(
                "sandbox destroyed",
                extra={"run_id": handle.run_id, "job_name": handle.job_name},
            )

    # -- kubernetes plumbing ----------------------------------------------

    def _delete_job(self, name: str, wait: bool = True) -> bool:
        """Delete a Job and (by default) wait until it is fully gone.
        Idempotent: returns False when there was nothing to delete."""
        try:
            self._batch.delete_namespaced_job(
                name, self._namespace, propagation_policy="Foreground"
            )
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        if wait:
            # Foreground propagation keeps the Job object until its pods are
            # gone, so "Job 404" is also "no pod left".
            deadline = time.monotonic() + DELETE_TIMEOUT_S
            while True:
                try:
                    self._batch.read_namespaced_job(name, self._namespace)
                except ApiException as exc:
                    if exc.status == 404:
                        break
                    raise
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"job {name} not deleted after {DELETE_TIMEOUT_S}s"
                    )
                time.sleep(1)
        return True

    def _wait_pod_running(self, name: str) -> k8s.V1Pod:
        deadline = time.monotonic() + POD_START_TIMEOUT_S
        while True:
            pods = self._core.list_namespaced_pod(
                self._namespace, label_selector=f"job-name={name}"
            ).items
            for pod in pods:
                if pod.metadata.deletion_timestamp is not None:
                    continue
                if pod.status.phase in ("Failed", "Succeeded"):
                    raise RuntimeError(
                        f"sandbox pod {pod.metadata.name} terminated during "
                        f"provisioning (phase {pod.status.phase})"
                    )
                if pod.status.phase == "Running" and pod.status.pod_ip:
                    return pod
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"no running sandbox pod for job {name} after {POD_START_TIMEOUT_S}s"
                )
            time.sleep(1)

    def _upload_workspace(
        self, pod_name: str, workspace_tar: bytes, workspace_dir: str
    ) -> None:
        """Deliver the workspace tar over the exec API and unpack it.

        The exec WebSocket client cannot half-close stdin, so `head -c` with
        the exact byte count stands in for EOF; base64 keeps the stream
        text-safe on the way through.
        """
        encoded = base64.b64encode(workspace_tar).decode()
        ws = stream(
            self._core.connect_get_namespaced_pod_exec,
            pod_name,
            self._namespace,
            command=["sh", "-c", f"head -c {len(encoded)} > {_UPLOAD_PATH}"],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
        )
        try:
            for i in range(0, len(encoded), UPLOAD_CHUNK):
                ws.write_stdin(encoded[i : i + UPLOAD_CHUNK])
            deadline = time.monotonic() + EXEC_TIMEOUT_S
            while ws.is_open():
                ws.update(timeout=1)
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"workspace upload not finished after {EXEC_TIMEOUT_S}s"
                    )
        finally:
            ws.close()
        if ws.returncode != 0:
            raise RuntimeError(f"workspace upload failed (exit {ws.returncode})")
        exit_code, output = self._exec(
            pod_name,
            [
                "sh",
                "-c",
                f"base64 -d {_UPLOAD_PATH} | tar -xf - -C {workspace_dir}"
                f" && rm {_UPLOAD_PATH}",
            ],
        )
        if exit_code != 0:
            raise RuntimeError(f"workspace unpack failed: {output.strip()}")

    def _exec(self, pod_name: str, command: list[str]) -> tuple[int, str]:
        ws = stream(
            self._core.connect_get_namespaced_pod_exec,
            pod_name,
            self._namespace,
            command=command,
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
        )
        chunks: list[str] = []
        try:
            deadline = time.monotonic() + EXEC_TIMEOUT_S
            while ws.is_open():
                ws.update(timeout=1)
                if ws.peek_stdout():
                    chunks.append(ws.read_stdout())
                if ws.peek_stderr():
                    chunks.append(ws.read_stderr())
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"exec {command[0]!r} not finished after {EXEC_TIMEOUT_S}s"
                    )
        finally:
            ws.close()
        return ws.returncode, "".join(chunks)
