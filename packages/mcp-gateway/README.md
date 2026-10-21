# mcp-gateway

The MCP gateway: the chokepoint every agent tool call passes through, and
**the only container that holds `GIT_TOKEN`**. An agent never sees a
credential; it asks the gateway to do something, and the gateway
authenticates it, decides, and acts on its behalf — leaving exactly one
audit event either way.

This is where the system's thesis becomes mechanical. The agent is not
trusted to stay in scope; it is *unable* to leave one, because the tool
surface is the only way out and every call on it is policy-checked against
a compiled bundle before any side effect happens. If the root README's
[the core idea](../../docs/argument/core-idea.md) is the argument, this
package is the working part: a reference monitor for agent effects, small
enough to read in an afternoon.

## The pipeline

Every call — MCP tool or admin route — goes through `GatewayService.call`
in `service.py`:

```
workload authn  (once per HTTP request, in middleware)
  -> build decision input  (+ verified run binding, injected live state)
  -> OPA decision on the caller's compiled path
  -> deny?  audit the denial; raise. The backend is never invoked.
  -> allow: run the backend, then audit with backend_status ok|error
```

Two properties matter here, and it is worth being precise about which is
which. **Deny before side effect** is an ordering guarantee: a denied call
is audited and never reaches a backend (`backend_status` is null on deny
events — there was no backend to have a status). **One audit event per
call** is a completeness guarantee: allowed calls are audited *after* the
backend runs, so the event can record whether the action actually
succeeded. An allowed call whose backend then fails is audited with
`backend_status: "error"`, and the tests assert all three shapes
(deny/null, allow/ok, allow/error).

The audit event does not store the caller's arguments — it stores
`args_digest`, a SHA-256 over their canonical JSON: enough to prove
*what* was asked without retaining file contents or PR bodies in the
audit table.

Authentication itself lives in `common.authn` (see the
[common README](../common/README.md)) but is exercised hard by this
package's `test_authn.py`: every credential is a projected ServiceAccount
token resolving through Kubernetes TokenReview (cached, one review per
token — but the registry mapping is re-read per call), and an ephemeral
ephemeral agent's SA additionally requires a **live** `run_participants` row —
a verified token alone is never enough, so deleting the row revokes the
identity however long the token remains valid. Every rejected token lands in the audit store
under an `authn_*` rule, with one deliberate exception: a request with
*no* credential gets a 401 but no audit event — probes and scanners send
no token, and only presented tokens are governance signals.

## The decision input — why the gateway builds it

OPA is a pure function of bundle + input. That is only a security property
if the *input* is trustworthy, so nothing in it that matters is taken from
the caller: the identity fields come from authn, the `workflow` comes from
the run row in the registry, and the run binding is verified server-side.
A caller that could supply its own "I am bound to this run" or "I have
zero live spawns" would make every compiled cap an honour system.

**Run binding.** The gateway resolves the claimed `run_id` against the
registry — the live sandbox registration and the run row — and injects
`run_binding_verified` into the input. The check depends on what kind of
caller this is (`_decision_input` in `service.py`):

- *Ephemeral* callers were bound to exactly one run when their
  participants row was written; verification is `identity.run_id ==
  run_id`.
- *Platform* callers are verified if a live registration exists.
- *Standing* callers get the subtle check: the run's sandbox must live in
  the caller's **own** namespace —

  ```python
  verified = (
      registration is not None
      and identity.namespace is not None
      and registration["namespace"] == identity.namespace
  )
  ```

  A live registration belonging to another company is a cross-tenant
  claim, not a binding.

Every compiled rule for a run-scoped tool (the seven `sandbox.*` tools —
`schema.tools.RUN_SCOPED`) requires the flag, so a wrong, stale or revoked
`run_id` fails closed at the policy layer. Retiring the registration at
teardown is what makes a finished run's sandbox unreachable: same caller,
same tool, now `default_deny`.

**Parentage.** An ephemeral identity missing its parent/crew coordinates
never reaches policy at all — `_decision_input` returns `None` and the
call is denied and audited as `ephemeral_parentage_missing`.

**Live counters.** For `architecture.request` the gateway additionally
injects `architecture_requests` — how many the run already filed, read
off the gateway's own audit trail — and for `git.open_pr` it computes
`paths_present`, which of the changed paths already exist on the base ref.
The compiled rules read them from the input — `architecture_request_over_cap`
and `git_open_pr_strategy_present` are rule denies like any other. (The
`crew.request` decision, with its spawn count and budget headroom, is the
orchestrator's to evaluate at fulfilment; it is not a gateway tool.)
Nothing is ever pushed into OPA, so a decision is replayable and an OPA
restart is safe: it re-polls the bundle, and the next input carries the
current state.

**Fail-closed, twice more.** If OPA has no document at the caller's
decision path, `opa.py` does not treat the empty answer as anything but a
deny:

```python
if result is None:
    # No document at the path: the company's bundle is not (or no
    # longer) installed. Fail closed — a torn-down company's callers
    # find no rule to match, which is what revocation means.
    return Decision(allow=False, rule="no_policy_installed")
```

And the decision path itself is per caller: platform identities query
`poseidon/platform/decision`, company callers query
`poseidon/authz/<slug>/decision`. Both paths, and the slug transform the
compiler emits bundles under, come from `common.policy` — one definition,
so the gateway cannot query a document the compiler never wrote.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The ASGI app: FastMCP server over streamable HTTP, the auth middleware, the tool registrations, and `POST`/`DELETE /admin/sandboxes`. |
| `service.py` | `GatewayService` — the pipeline above, plus one method per tool. |
| `opa.py` | The OPA client, the per-caller decision path, the fail-closed empty-document handling. |
| `git_backend.py` | Every platform-side git operation and the GitHub REST calls. The only place `GIT_TOKEN` is used. |
| `config.py` | `McpGatewaySettings`: the shared workload-authn shape plus `opa_url`, `git_token`, `base_ref`, `github_api_url`. |

## Tool surface

The tools — the sandbox tools proxied to the run's registered tool
server, `repo.archive` and `git.open_pr` backed by platform-side clones,
`repo.read_file` for the company's documents at the base ref, `repo.present`
for which declared paths the base ref holds, the issue
and review tools, `architecture.request`, and platform-only `git.merge_pr`,
`issues.close` and `git.close_pr` — are documented one by one, with arguments, on
[Tools](../../docs/reference/tools.md), rendered from this package's
definitions at build time.
The names match the lists in `schema.tools`, which is what the compiler
validates blueprint scopes against, so a scope naming a tool the gateway
does not serve fails at compile time.

Sandbox proxying is routing, not logic: `proxy_sandbox` looks up the
run's registered URL, strips `run_id` from the args (it addresses the
registry, not the sandbox) and POSTs to the tool server's route.

The admin routes register and retire a run's sandbox address. They live
behind the gateway rather than inside the orchestrator precisely so the
registration write is authenticated, policy-checked on the platform
decision path, and audited like everything else — the tests show an agent
token getting a 403 for the same request the orchestrator's is allowed.

## The git backend

`git.open_pr` is the only path by which code leaves the system, and the
agent never touches the remote. It supplies *content* — a git bundle, an
offline verifiable container of commits — and the gateway does everything
that carries authority:

1. **Verify and stage.** `prepare_pr` decodes the bundle, clones the
   target repo platform-side, runs `git bundle verify`, and fetches the
   bundle's branch onto a private ref (`refs/poseidon/incoming`) so it can
   never collide with a checked-out local branch.
2. **Extract the changed paths** with `git diff --name-only` against
   `origin/<base_ref>` — *before* the policy query, so `args.paths` are
   real observed facts in the decision input. This is what makes
   `deny_paths` and branch-protection rules enforceable rather than
   advisory: policy judges what the bundle actually changes, not what the
   caller says it changes.
3. **Decide, then push.** Only after `call` allows does the gateway push
   the branch under its own token and open the PR via the GitHub API.

The failure modes are the interesting part. A tampered or garbage bundle
makes `prepare_pr` raise — and `open_pr` still routes the failure through
`call` with a failing backend, so the attempt is audited rather than
lost. With no prepared bundle there are no changed paths, and the
compiled path rules fail closed without them: the recorded outcome is a
`git_open_pr_paths_missing` **deny**, nothing pushed, nothing sent to
GitHub. The dangerous condition is the absent input, and absence denies.

`repo.archive` is the reverse direction — how code *enters* a sandbox —
and it ends with `git remote remove origin` before tarring, so the
credentialed clone URL and all remote-tracking refs are gone from the
archive an agent receives. The test decodes the tarball and asserts the
token's bytes appear nowhere in it.

## How `GIT_TOKEN` is contained

- It appears in exactly two places: the clone URL of throwaway
  platform-side clones (`https://x-access-token:<token>@host/path`) and
  the GitHub API `Authorization` header.
- Every error path that could carry it is redacted: git subprocess
  failures, GitHub API errors, and the `subprocess.TimeoutExpired` case,
  whose own message would otherwise embed the full command line — clone
  URL included.
- The transport set is closed: local paths (tests) or `https` only.
  Git's other transports — `ext::` among them — can execute commands, so
  `ssh://`, `http://`, `ext::` and friends are refused outright.
- Caller-supplied `repo`, `ref` and `branch` are validated (no leading
  dash, no whitespace or control characters, no `::`, no
  refspec-splitting `:` or range-building `..`) and pinned positionally
  with `--` where git accepts it, so they can never be read as options.

`test_git_hardening.py` is the checklist for the last two bullets.

## Entry points

```
uvicorn --factory mcp_gateway.app:create_app --host 0.0.0.0 --port 8080
```

That is the `poseidon-mcp-gateway` entrypoint in `deploy/build-images.sh` verbatim; the image installs `git`
for the platform-side clones and runs as uid 10001. `create_app` opens
its own registry pool unless the tests inject in-memory accessors — all
or none, since injecting a subset would still open a real pool for the
rest. The Helm chart's readiness probe asserts a **401** from `/mcp`: no
unauthenticated route exists, so a 401 from the bearer middleware is what
proves the server is up.

## Cross-package interactions

- `common.authn` for identity, `common.audit` for the event model.
- `registry` for the run binding, audit sink, participants and ledger
  reads, and the audit store's own story of a run (the count of
  architecture requests already allowed under it). Every accessor is synchronous psycopg, so every use
  goes through `asyncio.to_thread` — a slow or absent Postgres must not
  stall the event loop (the pool blocks long enough to fail the liveness
  probe if it ran there).
- `schema.tools` names the same tools; `compiler.rego` must emit the
  same company slug — `tests/test_slug_invariant.py` pins that.
- The [orchestrator](../orchestrator/README.md) is the platform-lifecycle
  caller: admin routes, `repo.archive`, post-gate `git.merge_pr` and the
  closing acts that follow it (`issues.close`, `git.close_pr`).

## Tests

```
uv run pytest packages/mcp-gateway
```

A few seconds, no network and no cluster. The suite runs the
real ASGI app through a real MCP client over an in-process transport;
OPA, the sandbox tool server and GitHub are one scriptable
`httpx.MockTransport` (the OPA stub mirrors the *compiled* policy shape)
and TokenReview is faked. Git subprocesses are real, against a local bare
repo standing in for the target, so the bundle-verify, changed-paths and
push mechanics — token handling included — are exercised for real.

## Invariants

- Deny before side effect, always; exactly one audit event per call.
- Nothing is pushed to OPA; the registration row *is* the binding, and
  terminating it is revocation.
- Signature alone never authenticates an ephemeral caller — no live
  participants row, no identity.
- Parentage is mandatory for ephemeral callers — an identity missing it
  never reaches policy.
- `GIT_TOKEN` never leaves the gateway: not in archives, not in bundles,
  not in error messages, not in audit events.
- The gateway is unauthenticated to *nothing*: every route requires a
  workload credential, which is why its readiness probe asserts a 401.
