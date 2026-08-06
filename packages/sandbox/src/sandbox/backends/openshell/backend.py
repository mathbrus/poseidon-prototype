"""OpenShell backend — the evaluation track (plan step 24, O-5).

NVIDIA OpenShell behind contract v2 (C5), pinned and deliberately
declawed. **Nothing on the MVP critical path may reference this module**:
`k8s_jobs` is the default backend and this one exists to be measured, not
depended on (overview §4.6, "strictly non-gating").

What the adapter enforces, because the contract cannot assume the runtime
is well-behaved:

- **Zero credential providers.** OpenShell's providers mechanism injects
  secrets into sandboxes; a Poseidon sandbox holds no credential but its
  own run-scoped egress token (§2b), so `providers` is pinned empty and
  the adapter refuses any spec that would set one.
- **Inference routing never reaches a provider.** OpenShell can route
  model calls itself; that would bypass the metered LLM gateway (§4.8)
  entirely. The adapter leaves routing unconfigured, and the sandbox's
  only route out stays the audited egress proxy carried in `policy.env`.
- **Its Gateway reads nothing of ours.** The client is constructed with an
  endpoint and a bearer token, and is handed no blueprint, no registry
  DSN, no company spec.

The OpenShell client is imported lazily and lives behind the `openshell`
extra: the sandbox image runs untrusted workspace code and must not carry
this dependency (the same reason `registry` is an extra).
"""

import base64
import tarfile
import io
from typing import Any, Protocol

from common.telemetry import get_logger
from sandbox.contract import SandboxHandle, SandboxPolicy

# Pinned exactly (O-5 criterion: API stability under Helm pinning). OpenShell
# is a 0.0.x line that ships most days; an unpinned evaluation measures
# whatever shipped this morning.
PINNED_VERSION = "0.0.99"
# OpenShell groups sandboxes by "workspace" — its tenancy handle, unrelated
# to our per-run git workspace directory. One per company namespace.
DEFAULT_WORKSPACE = "poseidon"
RUN_ID_LABEL = "poseidon.io/run-id"


class OpenShellUnavailable(RuntimeError):
    """The optional dependency is not installed (this is an evaluation
    backend; the MVP path never imports it)."""


class ContractGap(RuntimeError):
    """OpenShell cannot satisfy something contract v2 requires. Raised
    loudly rather than papered over — the gaps are the evaluation's
    findings (see markdowns/MVP/cleaned/step24-openshell-eval.md)."""


class SandboxClientLike(Protocol):
    def create(self, *, workspace: str, spec: Any = None, name: str | None = None,
               labels: Any = None) -> Any: ...
    def wait_ready(self, sandbox_name: str, *, workspace: str,
                   timeout_seconds: float = 300.0) -> Any: ...
    def exec(self, sandbox_id: str, command: Any, **kwargs: Any) -> Any: ...
    def delete(self, sandbox_name: str, *, workspace: str) -> bool: ...


def sandbox_name(run_id: str) -> str:
    return f"poseidon-{run_id.lower()}"


def _load_client_module() -> Any:
    try:
        import openshell  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - import guard
        raise OpenShellUnavailable(
            "the openshell backend needs the `openshell` extra "
            f"(pinned {PINNED_VERSION}); it is deliberately absent from the "
            "sandbox image and the MVP critical path"
        ) from exc
    return openshell


def build_spec(policy: SandboxPolicy, module: Any) -> Any:
    """Map contract v2's `SandboxPolicy` onto an OpenShell `SandboxSpec`.

    Deliberate omissions are as load-bearing as the mappings: `providers`
    stays empty (no credential injection) and no inference route is
    configured (model calls belong to the metered LLM gateway).
    """
    pb = module._proto.openshell_pb2  # the generated spec types

    spec = pb.SandboxSpec()
    spec.template.image = policy.image
    # Landlock/user-namespace confinement and a hardened RuntimeClass are
    # OpenShell's headline isolation primitives — the ones §4.6 defers to
    # V1 for k8s_jobs. Left unset here on purpose: an evaluation that
    # silently enables them would be measuring a different posture than
    # the default backend's.
    for key, value in sorted(policy.env.items()):
        spec.environment[key] = value
    # Filesystem scope: the workspace is the only writable path.
    spec.policy.filesystem.include_workdir = True
    spec.policy.filesystem.read_write.append(policy.workspace_dir)
    resources = {
        "cpu": policy.cpu,
        "memory": policy.memory,
        "ephemeral-storage": policy.disk,
    }
    spec.template.resources.update(resources)
    if spec.providers:  # pragma: no cover - defensive
        raise ContractGap("credential providers must stay empty (§4.6)")
    return spec


class OpenShellBackend:
    """Contract v2 against a live OpenShell Gateway.

    `toolserver_url` is not optional and not derivable: see
    `provision` — it is the single biggest integration finding of the
    evaluation.
    """

    def __init__(
        self,
        client: SandboxClientLike,
        *,
        workspace: str = DEFAULT_WORKSPACE,
        toolserver_url: str | None = None,
        ready_timeout_s: float = 300.0,
        spec_builder: Any = None,
    ) -> None:
        self._client = client
        self._workspace = workspace
        self._toolserver_url = toolserver_url
        self._ready_timeout_s = ready_timeout_s
        # Seam, not indirection for its own sake: the lifecycle is testable
        # without OpenShell installed, while the default builder keeps the
        # policy mapping (and the declawing) on the real path.
        self._spec_builder = spec_builder or self._default_spec
        self._log = get_logger("sandbox.openshell")

    @staticmethod
    def _default_spec(policy: SandboxPolicy) -> Any:
        return build_spec(policy, _load_client_module())

    @classmethod
    def from_endpoint(
        cls, endpoint: str, *, bearer_token: str | None = None, **kwargs: Any
    ) -> "OpenShellBackend":
        module = _load_client_module()
        client = module.SandboxClient(endpoint, bearer_token=bearer_token)
        return cls(client, **kwargs)

    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle:
        """Create the sandbox, deliver the workspace, hand back a handle.

        **The contract gap this backend exists to measure:** C4 requires
        the run's toolserver to be reachable by the MCP gateway at an HTTP
        URL. OpenShell publishes no address for a sandbox — `SandboxRef`
        carries `{id, name, workspace, status, labels}` and every
        interaction is mediated by its Gateway's gRPC `exec`. So a
        deployment must run a C4-speaking shim that translates HTTP tool
        calls into `exec` calls, and `toolserver_url` is that shim's
        address. Refusing to invent one is the point: silently returning a
        dead URL would make the evaluation look cheaper than it is.
        """
        if self._toolserver_url is None:
            raise ContractGap(
                "OpenShell exposes no per-sandbox address, so contract C4's "
                "toolserver URL cannot be derived from a SandboxRef; a "
                "translating shim must be deployed and its address passed "
                "as toolserver_url (see the step-24 evaluation report)"
            )
        name = sandbox_name(run_id)
        spec = self._spec_builder(policy)
        ref = self._client.create(
            workspace=self._workspace,
            spec=spec,
            name=name,
            labels={RUN_ID_LABEL: run_id},
        )
        self._client.wait_ready(
            name, workspace=self._workspace, timeout_seconds=self._ready_timeout_s
        )
        self._deliver_workspace(ref.id, workspace_tar, policy.workspace_dir)
        self._log.info(
            "openshell sandbox provisioned",
            extra={"run_id": run_id, "sandbox": name},
        )
        return SandboxHandle(
            run_id=run_id,
            url=self._toolserver_url,
            namespace=self._workspace,
            job_name=name,
            pod_name=ref.id,
        )

    def destroy(self, handle: SandboxHandle) -> None:
        """Idempotent, like the k8s_jobs backend: a second destroy of an
        already-gone sandbox is a no-op, because teardown runs on every
        terminal path and may run twice."""
        name = handle.job_name or sandbox_name(handle.run_id)
        deleted = self._client.delete(name, workspace=self._workspace)
        self._log.info(
            "openshell sandbox destroyed",
            extra={"run_id": handle.run_id, "sandbox": name, "existed": bool(deleted)},
        )

    def _deliver_workspace(
        self, sandbox_id: str, workspace_tar: bytes, workspace_dir: str
    ) -> None:
        """Unpack the run's workspace inside the sandbox.

        OpenShell's `exec` takes real `stdin` bytes, so unlike the k8s_jobs
        backend (which base64s through the pods/exec websocket because the
        client cannot half-close stdin) the tar can be piped straight in.
        One of the few places OpenShell is simply nicer.
        """
        result = self._client.exec(
            sandbox_id,
            ["sh", "-c", f"mkdir -p {workspace_dir} && tar -x -C {workspace_dir}"],
            stdin=workspace_tar,
        )
        if result.exit_code != 0:
            raise ContractGap(
                f"workspace delivery failed ({result.exit_code}): {result.stderr}"
            )
