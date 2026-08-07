# gateway

The MCP gateway: the chokepoint every agent tool call passes through, and
**the only pod that holds `GIT_TOKEN`**. An agent never sees a credential;
it asks the gateway to do something, and the gateway decides, records, and
then acts — in that order.

This is where the system's thesis becomes mechanical. The agent is not
trusted to stay in scope; it is *unable* to leave one, because the tool
surface is the only way out and every call on it is policy-checked against a
compiled bundle before any side effect happens.

## The pipeline

Every call — MCP tool or admin route — goes through `GatewayService.call`:

```
workload authn  ->  decision input (+ verified run binding)
                ->  OPA decision on the caller's compiled path
                ->  audit event
                ->  backend action (only on allow)
```

The order is the point. A denied call is audited and never reaches a
backend. An allowed call whose backend then fails is audited too, with
`backend_status: error`.

## Run-binding enrichment

The decision input is not just what the caller claimed. The gateway resolves
the claimed `run_id` against the registry — live sandbox registration, its
namespace, the run row — and injects `run_binding_verified` into the
*input*. Nothing is ever pushed into OPA, so policy stays a pure function of
bundle + input and restarts are safe.

Standing callers get a stricter check than it first appears: the run's
sandbox must live in the caller's *own* namespace. A live registration
belonging to another company is a cross-tenant claim, not a binding.

Every `sandbox.*` rule requires the flag, so a wrong, stale or revoked
`run_id` fails closed at the policy layer.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The ASGI app: MCP server over streamable HTTP, the auth middleware, the 16 tools, and `POST`/`DELETE /admin/sandboxes`. |
| `service.py` | `GatewayService` — the pipeline above, plus one method per tool. |
| `opa.py` | The OPA client and the per-caller decision path. |
| `git_backend.py` | Every platform-side git operation and the GitHub REST calls. The only place `GIT_TOKEN` is used. |
| `config.py` | Settings: the shared workload-authn shape plus gateway-internal knobs. |

## Tool surface

Six sandbox tools proxied to the run's registered tool server
(`read_file`, `write_file`, `list_dir`, `exec`, `run_tests`, `git_bundle`);
`repo.archive` and `git.open_pr` backed by platform-side clones; the issue
and review tools (`issues.*`, `git.read`, `git.comment`); `git.merge_pr`,
which policy restricts to the platform lifecycle after the human merge gate;
and `crew.request`, which records spawn intent.

The admin routes register and retire a run's sandbox address. They live
behind the gateway rather than in the orchestrator precisely so that the
registration write is policy-checked and audited like everything else.

## How `GIT_TOKEN` is contained

- It appears only in the clone URL of throwaway platform-side clones and in
  the GitHub API header.
- `repo.archive` strips the remote before tarring, so the credentialed URL
  never reaches the archive an agent receives.
- Redaction covers every error and log path, including the
  `subprocess.TimeoutExpired` case, whose own message would otherwise carry
  the full command line.
- The transport set is closed: local paths or `https` only. Git's other
  transports — `ext::` among them — can execute commands.
- Caller-supplied `repo`, `ref` and `branch` are validated (no leading dash,
  no whitespace, no `::`, no refspec-splitting `:`) and pinned positionally
  with `--` where git accepts it.

## Entry points

```
uvicorn --factory gateway.app:create_app --host 0.0.0.0 --port 8080
```

`Dockerfile.gateway`. It installs `git`, which the platform-side clones need.

## Cross-package interactions

- `common.authn` for identity, `common.audit` for the event model.
- `registry` for the run binding, audit sink, crew requests, participants
  and ledger reads.
- Serves exactly the tool names `schema.tools` defines.
- Its `company_decision_path` slug must equal `compiler.rego.company_package`
  — `tests/test_slug_invariant.py` pins that across the two packages.

## Tests

```
uv run pytest packages/gateway
```

The suite runs the real ASGI app over an in-process transport with a local
bare git repo standing in for the target repo and a fake TokenReview, so the
authn, policy, audit and git paths are all exercised for real. `test_git_hardening.py`
covers the argument and transport validation above.

## Invariants

- Deny before side effect, always.
- Nothing is pushed to OPA; the registration row *is* the binding.
- Parentage is mandatory for ephemeral callers — an identity missing it
  never reaches policy.
- Registry access goes through `asyncio.to_thread`: the accessors are
  synchronous psycopg, and a slow or absent Postgres must not stall the
  event loop (the pool blocks long enough to fail the liveness probe).
- The gateway is unauthenticated to *nothing*: every route requires a
  workload credential, which is why its readiness probe asserts a 401.
