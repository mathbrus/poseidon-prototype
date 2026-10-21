# sandbox

Where untrusted code actually runs, and the layers that keep it from
mattering. Three things live here: the `SandboxBackend` contract and its
Kubernetes-Jobs implementation, the tool server that runs *inside* a
sandbox, and the egress proxy that is a sandbox's only route out.

The containment claim is deliberately narrow, and stating it precisely is
half the design. It is **not** "the code cannot escape the container" —
there is no gVisor or Kata RuntimeClass, so the container's privilege
boundary is not claimed as a security boundary (the root README's
[limitations](../../docs/explanation/limitations.md) say the same thing).
The claim is "privilege inside the sandbox reaches nothing": no
credential to scrape, no Kubernetes API or node metadata to reach, no
runtime socket or host mount, no other tenant's processes, and no route
out except one audited proxy. That is a claim about what *surrounds* the
container, not about the container itself — which is why every assertion
in the red-team suite (`tests/e2e_kind/test_redteam.py`, which attacks a
real provisioned sandbox) is written to hold at any uid: the non-root
posture is defence in depth on top of the containment, not the
containment.

## Module map

| Module | Owns |
|---|---|
| `contract.py` | `SandboxBackend`, `SandboxPolicy`, `SandboxHandle` — the narrow seam behind which runtimes are swapped. |
| `k8s_jobs.py` | The backend: one Job per run, workspace delivered over the Kubernetes exec API. |
| `manifest.py` | The Job manifest as a pure function of the policy — caps, deadline, workspace scope, env injection, security context. |
| `toolserver/app.py` | The in-sandbox HTTP server the gateway proxies the seven `sandbox.*` tools to. |
| `egressproxy/proxy.py` | The forward proxy: host allowlist, per-run attribution, an audit event for every decision. |
| `egressproxy/credentials.py` | The self-authenticating per-run credential, and `resolve_host`. |
| `egressproxy/__main__.py` | The proxy's process entry point; env-configured. |

## The contract: two methods, everything compiled in

`SandboxBackend` is a `Protocol` with exactly two methods:

```python
class SandboxBackend(Protocol):
    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle: ...

    def destroy(self, handle: SandboxHandle) -> None: ...
```

Everything a sandbox is allowed to be arrives in the `SandboxPolicy`
argument: image, egress allowlist, workspace directory, CPU/memory/disk
caps, wall-clock deadline, and a per-run `env` dict. Those values are
the sandbox projection of the applied blueprint's infrastructure IR —
the orchestrator forwards what the compiler emitted, so a different
backend gets the *same* policy object and swapping runtimes cannot
silently change what is enforced. The `env` field is the injection point
through which the orchestrator delivers the per-run egress credential
(see below) at provision time, before the pod runs an instruction.

## The k8s_jobs backend

One Kubernetes Job per run, in the company namespace. Provisioning is a
sequence, and each step has a reason:

1. **Delete any remnant Job of the same name.** The orchestrator is
   durable (Temporal): a crashed earlier attempt may have left a Job
   behind, and since there is one provision per run the remnant can only
   be our own. Deleting first makes the activity retry-safe.
2. **Create the Job and wait for a Running pod with an IP.** The pod is
   addressed by IP, not by a Service — the gateway's egress
   NetworkPolicy admits it by pod selector.
3. **Upload the workspace tarball over the Kubernetes exec API** — the
   same API-server path `kubectl exec` uses. This is the design decision
   worth pausing on: delivery over exec needs **no ingress exception**,
   so the sandbox NetworkPolicy can keep ingress gateway-only and egress
   proxy-only from the moment the pod exists.
4. **Prepare the workspace**: strip every git remote — no credentialed
   URL may survive inside a sandbox — and record `git rev-parse HEAD`
   into `.poseidon-base`, the base ref later `git bundle` calls diff
   against.
5. **Probe the tool server from inside the pod** (pod IPs are not
   generally routable from the provisioning host) until it answers.
   `provision` must not return early: the agent's first proxied call is
   not retried.

Any failure tears the Job down before the exception propagates — a
half-provisioned sandbox never outlives its provisioning attempt.

A Job is a slightly odd shape for a server, and the manifest says so:
`backoffLimit: 0` and `restartPolicy: Never`, because a pod that dies
mid-run must not be restarted into a half-broken workspace — the
provision activity's retry re-provisions from scratch. What the Job
*does* buy is `activeDeadlineSeconds`: a wall-clock deadline enforced by
Kubernetes itself, so a runaway stops burning compute on its own rather
than living until someone notices. `destroy` deletes with foreground
propagation and waits for the 404, so "Job gone" also means "no pod
left".

Only the orchestrator calls `provision`/`destroy`; its ServiceAccount
carries a bounded, namespaced Role (jobs, pods, pods/exec in company
namespaces only — see the reconciler's `sandbox_provisioner_role`).

## What the sandbox pod does not have

The manifest (`manifest.py`, a pure function of the policy, unit-tested
field by field) is mostly a list of absences:

- `automountServiceAccountToken: False` — no Kubernetes identity, and
  no route to the API server anyway.
- `enableServiceLinks: False` — no service-discovery env leaking
  cluster topology into the pod.
- uid/gid 10001 (`runAsNonRoot`), `allowPrivilegeEscalation: False`,
  all capabilities dropped, default seccomp filter — shared constants
  from `common.podsecurity`, so tightening the posture cannot skip one
  of the three pod builders in the codebase.
- Requests equal to limits for CPU, memory and ephemeral-storage. The
  root filesystem stays writable on purpose — workspace code
  legitimately writes (git worktrees, `pip --user` installs) — and the
  ephemeral-storage limit caps what that can fill.
- Even `GPG_KEY` — a public release-signing fingerprint that
  `python:3.12-slim` bakes into every image — is blanked, so no
  token-shaped value exists in a company-namespace pod env at all.

## The tool server is unauthenticated, on purpose

The FastAPI app in `toolserver/app.py` exposes seven endpoints, one per
`sandbox.*` tool the MCP gateway offers: `/files/read`, `/files/write`,
`/files/edit`, `/files/list`, `/exec`, `/tests`, `/bundle`. It has no authentication,
and `/exec` runs what it is given with `shell=True`. That is not an
oversight: it *is* the sandbox — the thing whose whole job is to run
untrusted code. Its protection is that nothing else can reach it: the
sandbox NetworkPolicy admits ingress from the MCP gateway alone, and the
gateway authenticates and policy-checks every call before proxying. This
is one of the unauthenticated-by-design surfaces the root README's
[what it cannot do](../../docs/explanation/limitations.md) names explicitly, and
it depends on a CNI that enforces NetworkPolicy (Calico on kind).

What the server *does* enforce is filesystem scope. Every path resolves
inside the workspace:

```python
def resolve(path: str) -> Path:
    candidate = (ws / path).resolve()
    if not candidate.is_relative_to(ws):
        raise HTTPException(status_code=400, detail=f"path escapes workspace: {path}")
    return candidate
```

`.resolve()` before the containment check is what defeats both `../`
traversal and symlink escapes — the unit tests try both. Two more
conventions: a timed-out `/exec` returns exit code 124 (the coreutils
`timeout` convention), and `/tests` runs `TEST_CMD` (default `pytest
-q`) with a fixed 600-second ceiling — that endpoint is what the
orchestrator's verify gate calls under its own identity, so its
behaviour must not depend on anything the agent set up.

`/bundle` is the delivery half of the design: it produces a git bundle
of `base_ref..branch`, where `base_ref` comes from the `.poseidon-base`
file written at provision time. The sandbox has no remote to push to —
the bundle is inert content that the *gateway* verifies, applies to a
platform-side clone, and pushes with its own token (see
[anatomy of a run](../../docs/explanation/anatomy.md)).

## Egress: allowlisted, attributed, audited

The proxy is owned rather than off-the-shelf because the audit contract
*is* the point. NetworkPolicy drops packets silently and stays the
backstop beneath it; the proxy is what makes "egress attempt blocked and
visible in the audit store" mechanically true. It speaks HTTP CONNECT
for TLS and absolute-form plain HTTP; the only origin-form request it
answers is `GET /healthz` for the kubelet probe, which is deliberately
not an audit event.

Every tunnel starts with attribution. The credential is
self-authenticating — username is the `run_id`, password is
`HMAC(key, run_id)`:

```python
def mint(key: str, run_id: str) -> str:
    return hmac.new(key.encode(), run_id.encode(), hashlib.sha256).hexdigest()


def verify(key: str, run_id: str, secret: str) -> bool:
    return hmac.compare_digest(mint(key, run_id), secret)
```

The orchestrator holds the shared key and mints the credential into the
sandbox environment at provision time, as the standard
`HTTP_PROXY`/`HTTPS_PROXY` variables that pip, npm and curl honor with
no sandbox-side setup. The proxy verifies statelessly and maps the
credential back to the run: no registration round-trip, nothing lost on
a proxy restart, and unforgeable from inside a sandbox that only ever
sees its own. A missing or bad credential is a 407; a verified caller
asking for a host off the list is a 403 — and on a bad credential the
*claimed* run_id is still recorded, so failed attribution attempts stay
visible rather than anonymous. Every decision — allow or deny — lands
in the registry's `audit_events` table with `source=sandbox-infra`,
correlated by `run_id` with the gateway's events.

The allowlist is the applied blueprint's compiled IR projection,
resolved per request: run → live sandbox registration → company (by
namespace) → the blueprint's `egress_proxy_allowlist`. **No applied
blueprint or no live registration means zero egress** — which is also
how retiring a registration revokes egress mid-run, within the
resolver's short cache window (10 seconds, kept so a chatty
`pip install` costs two registry queries rather than two per
connection).

### The sandbox has no DNS

Cluster DNS is not in the sandbox's NetworkPolicy egress, because a
resolver that forwards unknown names upstream is a channel the audited
proxy cannot see. Nothing in a sandbox needs one: with an HTTP proxy the
*proxy* resolves target hosts, and the one name the sandbox would have
needed — the proxy's own address — is resolved by the orchestrator at
provision time (`credentials.resolve_host`). That helper raises
`ProxyUnresolvable` rather than passing a hostname through: handing a
resolver-less sandbox a name guarantees a DNS failure inside pip or
curl, as far from the misconfiguration as possible — raising fails the
provisioning activity instead, where there is a resolver, a log and a
retry.

## Entry points

| What | How | Image |
|---|---|---|
| tool server | `uvicorn sandbox.toolserver.app:app --host 0.0.0.0 --port 8000` | `poseidon-sandbox` |
| egress proxy | `python -m sandbox.egressproxy` | `poseidon-egress-proxy` |
| backend | library, called by the orchestrator | ships in `poseidon-orchestrator` |

The sandbox image prebakes its toolchain (pytest via the `toolchain`
extra) because sandbox egress is proxy-only and an empty allowlist means
none at all. The `egress` extra carries the registry driver for the
proxy — kept an extra so the *sandbox* image, which runs untrusted code,
never carries a database driver or registry credentials.

## Cross-package interactions

- **orchestrator** is the only caller of `provision`/`destroy`; it also
  calls `sandbox_proxy_env` to mint the egress credential into
  `SandboxPolicy.env`.
- **gateway** proxies the seven `sandbox.*` tools to the tool server
  (after authenticating, policy-checking and auditing each call) and is
  the only ingress the sandbox NetworkPolicy admits.
- **reconciler** emits that NetworkPolicy (`sandbox-isolation`) and the
  orchestrator's bounded provisioning Role.
- **registry** supplies the proxy's allowlist resolution and receives
  its audit events; **compiler** produces the infra IR the policy and
  allowlist are projections of.

## One backend

`k8s_jobs` is the only backend. The `SandboxBackend` Protocol exists for
the seam, not for a second runtime: provisioning takes a compiled
`SandboxPolicy` — the compiler's infra IR — and never hand-authored
values, and the Protocol is what pins that shape.

## Tests

```
uv run pytest packages/sandbox
```

Unit tests cover the Job manifest (every `SandboxPolicy` cap and scope
must land in the spec), the tool server's endpoints and path confinement
(traversal and symlink escapes), and the proxy's
allow/deny/credential paths over real sockets on localhost, with the
audit store asserted per decision. The live proofs — a runaway actually
killed, egress actually blocked and audited, the red-team suite — are in
`tests/e2e_kind/`.

## Invariants

- The sandbox holds no Kubernetes identity: `automountServiceAccountToken:
  False`, and there is no route to the API server anyway.
- It runs as uid 10001 with no privilege-escalation path and no
  capabilities — but every red-team containment assertion holds at any
  uid; non-root is depth, not the boundary.
- `activeDeadlineSeconds` on every Job: a runaway stops burning compute
  on its own rather than living until someone notices.
- `backoffLimit: 0` and `restartPolicy: Never` — a pod that dies mid-run
  is not restarted into a half-broken workspace; the provision activity's
  retry re-provisions from scratch.
- The workspace clone has its remote stripped at provision time, so no
  credentialed URL survives inside a sandbox.
- The egress proxy audits **every** decision it makes, allow and deny
  alike, including denials of unverified callers under their claimed
  run_id. Only the health probe is exempt.
