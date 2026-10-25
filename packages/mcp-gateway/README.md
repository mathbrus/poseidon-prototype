# mcp-gateway

The MCP gateway is the chokepoint every agent tool call passes through, and the only container that holds `GIT_TOKEN`. Agents and the orchestrator call it over MCP (streamable HTTP) in the platform namespace.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-mcp-gateway` | `uvicorn --factory mcp_gateway.app:create_app --port 8080` | `GIT_TOKEN`, `REGISTRY_DSN` | `audit_events`, `sandbox_registrations`, `filed_tickets` |

## Module map

| Module | Owns |
|---|---|
| `app.py` | The ASGI app: the FastMCP server, the auth middleware, the tools and `POST`/`DELETE /admin/sandboxes`. |
| `service.py` | `GatewayService`: the enforcement pipeline (`call`), the decision input, one method per tool, and the denial explanations (`explain_denial`). |
| `opa.py` | The OPA client. An empty answer at the caller's decision path is a deny, `no_policy_installed`. |
| `git_backend.py` | Platform-side clones, bundle verification, GitHub REST calls, argument checks. The only reader of `GIT_TOKEN`. |
| `config.py` | `McpGatewaySettings`: the shared workload-authn fields plus `opa_url` and `git_token`. |

Authentication lives in `common.authn` ([common](../common/README.md)). The tools are listed on [Tools](../../docs/reference/tools.md), rendered from `app.py`. Their names match `blueprint.tools`, so a scope naming an unknown tool fails to compile.

## How it works

### Every call is decided before it acts

Every MCP tool and admin route goes through `GatewayService.call`:

```
workload authn            once per HTTP request, in the middleware
-> rationale check        committing tools only; refused here when missing
-> decision input         identity, args, verified run binding, live facts
-> OPA decision           on the caller's decision path
-> deny                   audit, raise PolicyDenied; the backend never runs
-> allow                  run the backend, then audit with backend_status ok or error
```

A decided call leaves exactly one audit event, with a null `backend_status` on a deny. The event stores `args_digest`, a SHA-256 of the arguments, never the arguments.

The committing tools (`blueprint.tools.RATIONALE_TOOLS`) owe a rationale. A call without one is refused before policy and leaves no row. The rationale is stored on the event, cut to 1000 characters.

A denial answers `denied by policy rule: <rule>`. For repository, branch, path and document rules, `explain_denial` adds what was refused in the applied blueprint's words. The audit row keeps the bare rule.

The platform identity queries `poseidon/platform/decision`, and company callers query `poseidon/authz/<slug>/decision`, both from `common.policy`. No route is open, so the readiness probe expects a 401 from `/mcp`.

### The gateway builds the decision input

The identity comes from authn and the `workflow` from the run's row, never from the caller. `_decision_input` verifies the claimed `run_id` and sets `run_binding_verified`:

- An ephemeral agent is verified when the run matches the one its `run_participants` row binds it to.
- The platform identity is verified when the run has a live sandbox registration.
- A standing agent is verified when the run's live registration is in the agent's own namespace.

Every rule for a run-scoped tool (the seven `sandbox.*` tools) requires the flag. `DELETE /admin/sandboxes` retires a registration, and every later call for that run fails closed. An ephemeral identity without its parent or crew is denied as `ephemeral_parentage_missing` before OPA is asked.

It also injects live facts: `architecture_requests`, counted from its own audit rows, and the `paths` and `paths_present` of a `git.open_pr` bundle. Nothing is pushed into OPA, so a decision depends only on the bundle and the input.

### Code leaves only through `git.open_pr`

The agent supplies only a git bundle:

1. `prepare_pr` decodes the bundle, clones the repository, runs `git bundle verify` and fetches the branch onto `refs/poseidon/incoming`.
2. It diffs against the applied blueprint's `base_ref`. The changed paths become `args.paths`, and those already on the base ref become `paths_present`.
3. `call` decides. Only on allow does the gateway push the branch and open the pull request.

A bundle that fails verification still goes through `call`, with no changed paths, and is denied as `git_open_pr_paths_missing`. `repo.archive` works the other way: it clones, applies any overlay or merges the ref onto `merge_onto` as the platform, removes the remote and returns a tarball. A merge that conflicts is an error starting `merge conflict`.

### Closing and dismissing say why on the host

`issues.close`, `issues.dismiss` and `git.close_pr` post their rationale as a comment on the item before closing it, and leave an item already closed as it is. `issues.close` takes `reason` (`completed` or `not_planned`); `git.close_pr` may delete the head branch (`delete_branch`). Before policy decides `issues.dismiss`, `_dismiss_facts` adds what its compiled limits read: `run_live`, `dismissals` (off the gateway's own audit trail, like `architecture_requests`) and `dismiss_target`. The ticket is read only on the company's own repository; one that cannot be read leaves its facts out, and the call is denied.

`GIT_TOKEN` stays inside the gateway:

- It appears only in throwaway clone URLs and the GitHub API `Authorization` header.
- Git failures, GitHub API errors and git timeouts are redacted.
- Only local paths and `https` are accepted; `ssh://`, `http://` and `ext::` are refused.
- Caller-supplied repos, refs and paths cannot start with a dash or carry whitespace, control characters or `::`. Refs also refuse `:` and `..`, and overlay paths refuse `..`.
- Archives are tarred after removing the remote.

## Invariants

- Deny before side effect: `test_git_tools.py::test_pr_to_main_is_denied_and_nothing_is_pushed`.
- One audit event per decided call: `test_gateway.py::test_every_call_appends_one_ordered_audit_line`.
- A retired registration is a stale binding: `test_gateway.py::test_terminated_registration_is_a_stale_binding`.
- A terminated participant row revokes before the token expires: `test_authn.py::test_row_termination_revokes_before_ttl`.
- Policy judges the paths the bundle changes: `test_git_tools.py::test_changed_paths_are_part_of_the_policy_input`.
- A tampered bundle is denied and audited: `test_git_tools.py::test_tampered_bundle_errors_and_is_audited`.
- Archives carry no token: `test_git_tools.py::test_platform_archives_repo_stripped_of_remotes`.
- Unsafe repos, refs and paths fail: `test_git_hardening.py`.
- Merging and closing are platform-only: `test_tool_surface.py::test_merge_is_platform_only`.
- Only the console may purge, and a purge pages through every listing and refuses before anything changes on a protected branch: `test_repo_purge.py`.
- A call owing a rationale is refused without one: `test_tool_surface.py::test_a_ticket_without_a_rationale_is_refused_before_policy`.

## Testing

```bash
uv run pytest packages/mcp-gateway
```

It needs no network, cluster or Postgres (see [Contributing](../../docs/developing/contributing.md)). A real MCP client drives the real app. OPA, the tool server and GitHub are one `httpx.MockTransport`, TokenReview is faked and the registry is in memory. Git runs for real against a local bare repository. On kind, `test_governance.py`, `test_workload_identity.py` and `test_ephemeral_identity.py` drive the deployed gateway.

## Related

- [Security model](../../docs/explanation/security-model.md)
- [MCP](../../docs/concepts/mcp.md), [OPA](../../docs/concepts/opa.md), [Workload identity](../../docs/concepts/workload-identity.md)
- [Policy](../../docs/reference/policy.md)
- [orchestrator](../orchestrator/README.md), the platform caller
