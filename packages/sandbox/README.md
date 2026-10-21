# sandbox

The sandbox package holds three parts: the `SandboxBackend` contract with its Kubernetes Jobs backend, the tool server that runs inside each sandbox, and the egress proxy that is a sandbox's only route out. The orchestrator provisions one sandbox per run in the company namespace, and the MCP gateway proxies the `sandbox.*` tools to it.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service (tool server) | `poseidon-sandbox` | `uvicorn sandbox.toolserver.app:app --host 0.0.0.0 --port 8000` | nothing | nothing |
| service (egress proxy) | `poseidon-egress-proxy` | `python -m sandbox.egressproxy` | `EGRESS_PROXY_KEY`, `REGISTRY_DSN` | `audit_events` |
| library (backend) | ships in `poseidon-orchestrator` | `K8sJobsBackend` | nothing | nothing |

## Module map

| Module | Owns |
|---|---|
| `contract.py` | `SandboxBackend` (`provision`, `destroy`), `SandboxPolicy`, `SandboxHandle`. |
| `manifest.py` | The Job manifest as a pure function of the policy. |
| `k8s_jobs.py` | `K8sJobsBackend`: one Job per run, workspace delivered over the Kubernetes exec API. |
| `toolserver/app.py` | The in-sandbox HTTP server behind the seven `sandbox.*` tools. |
| `egressproxy/proxy.py` | `EgressProxy` and `RegistryAllowlist`: host allowlist, per-run attribution, an audit event per decision. |
| `egressproxy/credentials.py` | The per-run proxy credential (`mint`, `verify`), `resolve_host`, `sandbox_proxy_env`. |
| `egressproxy/__main__.py` | The proxy's process entry point. |

The `toolchain` extra prebakes pytest into the sandbox image. The `egress` extra adds the registry driver to the proxy image only.

## How it works

### A sandbox is one Job, provisioned from a compiled policy

`SandboxPolicy` carries the image, workspace directory, CPU, memory and disk caps, wall-clock deadline and a per-run `env`. The orchestrator passes the values the compiler emitted from the applied blueprint. `provision` then:

1. Deletes any remnant Job of the same name, so a retried activity starts clean.
2. Creates the Job and waits for a Running pod with an IP.
3. Uploads the workspace tarball over the Kubernetes exec API, so the sandbox needs no ingress exception.
4. Strips every git remote, records `HEAD` in `.poseidon-base`, and adds `.poseidon-base`, `__pycache__/` and `*.pyc` to `.git/info/exclude`.
5. Probes the tool server from inside the pod until it answers.

Any failure deletes the Job before the exception propagates. The Job has `backoffLimit: 0`, `restartPolicy: Never` and `activeDeadlineSeconds` from the policy. `destroy` deletes with foreground propagation and waits for the 404, so no pod is left. The orchestrator's Role for this is `sandbox_provisioner_role` in the [reconciler](../reconciler/README.md).

The sandbox pod does not have:

- a Kubernetes identity (`automountServiceAccountToken: false`);
- service-discovery env (`enableServiceLinks: false`);
- root, privilege escalation or capabilities (uid 10001, the shared contexts from `common.podsecurity`, default seccomp);
- resources beyond its limits (requests equal limits for CPU, memory and ephemeral storage);
- the base image's `GPG_KEY`, which is blanked;
- any credential, git remote or route out except the egress proxy;
- cluster DNS.

### The tool server confines every path to the workspace

The tool server exposes `/files/read`, `/files/write`, `/files/edit`, `/files/list`, `/exec`, `/tests` and `/bundle`. It has no authentication, and `/exec` runs any shell command. The sandbox NetworkPolicy admits ingress from the MCP gateway alone, and the gateway authenticates and checks policy first. See [Limitations](../../docs/explanation/limitations.md).

Every path resolves inside `WORKSPACE_DIR` before use, which blocks `../` traversal and symlink escapes with a 400. A timed-out `/exec` returns exit code 124. `/tests` runs `TEST_CMD` (default `pytest -q`) with a 600-second ceiling, appending any named `paths`. `/bundle` returns a git bundle of `base_ref..branch`, read from `.poseidon-base`. The gateway applies and pushes it with its own token.

### Egress goes through one audited proxy

The proxy handles HTTP CONNECT and absolute-form HTTP. Its credential is self-authenticating: the username is the `run_id`, the password is `HMAC(key, run_id)`. The orchestrator mints it into `SandboxPolicy.env` as `HTTP_PROXY` and `HTTPS_PROXY`, which pip, npm and curl honor.

| Case | Answer | Audit rule |
|---|---|---|
| no credential | 407 | `egress_no_credential` |
| bad credential | 407 | `egress_bad_credential`, under the claimed `run_id` |
| host not on the allowlist | 403 | `egress_not_allowlisted` |
| allowed, upstream unreachable | 502 | `egress_allowlisted`, `backend_status=error` |
| allowed | tunnel | `egress_allowlisted`, `backend_status=ok` |

Events land in `audit_events` with `tool=sandbox.egress` and `source=sandbox-infra`. `GET /healthz` answers without an audit event. The allowlist resolves run, live sandbox registration, company by namespace, then the blueprint's `egress_proxy_allowlist`, cached for 10 seconds. No registration or no applied blueprint means no egress.

The sandbox has no DNS. The proxy resolves target hosts, and the orchestrator resolves the proxy's own address at provision time with `resolve_host`. If that fails, `ProxyUnresolvable` fails the provisioning activity.

## Invariants

- Every policy cap, the deadline and the env land in the Job: `test_k8s_jobs_manifest.py::test_policy_caps_and_deadline_land_in_the_spec`, `test_workspace_scope_and_env_injection_seam`.
- The sandbox holds no Kubernetes identity: `test_k8s_jobs_manifest.py::test_sandbox_holds_no_kubernetes_identity`.
- It runs non-root with no escalation path: `test_k8s_jobs_manifest.py::test_sandbox_runs_non_root_with_no_escalation_path`.
- No git remote survives provisioning: `test_k8s_jobs.py::test_prepare_strips_every_git_remote_from_the_workspace`.
- Platform files stay out of deliveries: `test_k8s_jobs.py::test_prepare_keeps_platform_and_build_artifacts_out_of_deliveries`.
- No path escapes the workspace: `test_toolserver.py::test_write_symlink_escape_rejected`, `test_tests_endpoint_refuses_escaping_paths`.
- Every proxy decision is audited, failed attribution included: `test_egressproxy.py::test_bad_credential_is_denied_with_claimed_run_id`, `test_non_allowlisted_host_is_denied_and_audited`.
- No registration or blueprint means zero egress: `test_egressproxy.py::test_registry_allowlist_resolves_run_to_company_projection`.
- An unresolvable proxy fails provisioning: `test_egressproxy.py::test_unresolvable_proxy_host_fails_at_provision`.

## Testing

```bash
uv run pytest packages/sandbox
```

See [Contributing](../../docs/developing/contributing.md). No cluster or database is needed. The proxy tests use real sockets on localhost and an in-memory registry. The e2e suite covers provisioning in `tests/e2e_kind/test_sandbox_k8s.py`, `test_containment.py` and `test_redteam.py`.

## Related

- [Security model](../../docs/explanation/security-model.md), [Limitations](../../docs/explanation/limitations.md)
- [Network policy](../../docs/concepts/network-policy.md), [Blueprint reference](../../docs/reference/blueprint.md)
- [mcp-gateway](../mcp-gateway/README.md), [orchestrator](../orchestrator/README.md), [reconciler](../reconciler/README.md), [registry](../registry/README.md)
