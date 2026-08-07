# sandbox

Where untrusted code actually runs, and the layers that keep it from
mattering. Three things live here: the `SandboxBackend` contract and its
Kubernetes-Jobs implementation, the tool server that runs *inside* a
sandbox, and the egress proxy that is a sandbox's only route out.

The containment claim is deliberately narrow. It is **not** "the code cannot
escape the container" — there is no gVisor or Kata RuntimeClass, so the
container's privilege boundary is not claimed as a security boundary. It is
"privilege inside the sandbox reaches nothing": no credential to scrape, no
Kubernetes API or node metadata to reach, no runtime socket or host mount,
no other tenant's processes, and no route out except one audited proxy.
`tests/e2e_kind/test_redteam.py` attacks a real provisioned sandbox to prove
each of those.

## Module map

| Module | Owns |
|---|---|
| `contract.py` | `SandboxBackend`, `SandboxPolicy`, `SandboxHandle` — the narrow seam behind which runtimes are swapped. |
| `backends/k8s_jobs/backend.py` | The default backend: one Job per run, workspace delivered over the Kubernetes exec API. |
| `backends/k8s_jobs/manifest.py` | The Job manifest as a pure function of the policy — caps, deadline, workspace scope, env injection, security context. |
| `toolserver/app.py` | The in-sandbox HTTP server the gateway proxies the six `sandbox.*` tools to. |
| `egressproxy/proxy.py` | The forward proxy: host allowlist, per-run attribution, an audit event for every decision. |
| `egressproxy/credentials.py` | The self-authenticating per-run credential, and `resolve_host`. |

## Why the workspace arrives over the exec API

The tarball is delivered at startup over the Kubernetes exec API — the same
API-server path `kubectl exec` uses. That needs no ingress exception, so the
sandbox NetworkPolicy can keep ingress gateway-only and egress proxy-only
from the moment the pod exists.

## The tool server is unauthenticated, on purpose

It exposes `/exec` and runs what it is given with `shell=True`. That is not
an oversight: it *is* the sandbox — the thing whose whole job is to run
untrusted code. Its protection is that nothing can reach it. The sandbox
NetworkPolicy admits ingress from the MCP gateway alone, and the gateway
authenticates and policy-checks every call before proxying. This is one of
the surfaces the root README's limitations section names explicitly, and it
depends on a CNI that enforces NetworkPolicy (Calico on kind).

All paths resolve inside the workspace; a path that escapes it is a 400.

## Egress: allowlisted, attributed, audited

The proxy is owned rather than off-the-shelf because the audit contract *is*
the point. NetworkPolicy drops packets silently and stays the backstop
beneath it; the proxy is what makes "egress attempt blocked and visible in
the audit store" mechanically true.

The credential is self-authenticating: username is the `run_id`, password is
`HMAC(key, run_id)`. The orchestrator holds the shared key and mints it into
the sandbox environment at provision time; the proxy verifies statelessly
and maps it back to the run. No registration round-trip, nothing lost on a
proxy restart, and unforgeable from inside a sandbox that only ever sees its
own.

The allowlist is the applied blueprint's compiled IR projection, resolved
per run through the live sandbox registration. **No applied blueprint or no
live registration means zero egress** — which is also how retiring a
registration revokes egress mid-run.

### The sandbox has no DNS

Cluster DNS is not in the sandbox's NetworkPolicy egress, because a resolver
that forwards unknown names upstream is a channel the audited proxy cannot
see. Nothing in a sandbox needs one: with an HTTP proxy the *proxy* resolves
target hosts, and the one name the sandbox would have needed — the proxy's
own address — is resolved by the orchestrator at provision time
(`credentials.resolve_host`). That helper raises `ProxyUnresolvable` rather
than passing a hostname through: handing a resolver-less sandbox a name
guarantees a failure as far from the misconfiguration as possible.

## Entry points

| What | How | Image |
|---|---|---|
| tool server | `uvicorn sandbox.toolserver.app:app --host 0.0.0.0 --port 8000` | `Dockerfile.sandbox` |
| egress proxy | `python -m sandbox.egressproxy` | `Dockerfile.egressproxy` |
| backend | library, called by the orchestrator | ships in `Dockerfile.orchestrator` |

The sandbox image prebakes its toolchain (pytest via the `toolchain` extra)
because sandbox egress is proxy-only and an empty allowlist means none at
all. The `egress` extra carries the registry driver for the proxy — kept an
extra so the *sandbox* image, which runs untrusted code, never carries a
database driver or registry credentials.

## On OpenShell

An external sandbox runtime, OpenShell, was evaluated against this contract
and **not adopted**. The posture it suggested was borrowed; the backend was
removed rather than carried as dead code. The evaluation and the removal are
in git history if the reasoning is needed again.

## Tests

```
uv run pytest packages/sandbox
```

Unit tests cover the Job manifest (every `SandboxPolicy` cap and scope must
land in the spec) and the proxy's allow/deny/credential paths over real
sockets on localhost. The live proofs — a runaway actually killed, egress
actually blocked and audited, the red-team suite — are in
`tests/e2e_kind/`.

## Invariants

- The sandbox holds no Kubernetes identity: `automountServiceAccountToken:
  False`, and there is no route to the API server anyway.
- It runs as uid 10001 with no privilege-escalation path and no
  capabilities. The root filesystem stays writable because workspace code
  legitimately writes; the ephemeral-storage limit caps what that can fill.
- `activeDeadlineSeconds` on every Job: a runaway stops burning compute on
  its own rather than living until someone notices.
- `backoffLimit: 0` and `restartPolicy: Never` — a pod that dies mid-run is
  not restarted into a half-broken workspace; the provision activity's
  retry re-provisions from scratch.
- The workspace clone has its remote stripped, so no credentialed URL
  survives inside a sandbox.
