# Tools

The MCP gateway exposes <!-- gen: tool-count --> tools. They are the entire
action surface: everything any agent — or the platform itself — does to a
sandbox, the git host, or the issue tracker is one of these names. Every
call follows the same pipeline: authenticate the caller's workload
identity, build the policy input, ask OPA, and record exactly one audit
event. The ordering guarantee is *deny before side effect*: a denial is
audited and returned before anything happens, while an allowed call runs
the backend first and then writes its audit event carrying the backend's
outcome (`backend_status: ok|error`).

Three things to keep in mind while reading the signatures:

- **`run_id` appears in every signature but no model ever writes it.** The
  action schemas the model sees are `run_id`-free; the agent host injects
  it at call time, and the gateway independently verifies the claimed run
  against the caller's registry binding. A mismatch is a denial.
- **`repo` is likewise injected** by the agent host, never chosen by the
  model — and compiled policy denies any value other than the blueprint's
  `stack.repo.url` (`repo_not_declared`), so an agent works on its
  company's repository and nothing else.
- **Who may call what is compiled policy**, not a property of the tool.
  The last column is the catalog ceiling: the roles whose catalog entry
  holds the tool before a blueprint narrows it. A tool no role holds
  belongs to the platform lifecycle alone.

Three tools commit the company to something in the world — a ticket
(`issues.create`), a pull request (`git.open_pr`), a request to change the
architecture (`architecture.request`) — and take a `rationale`: the
caller's own reason, in a few sentences. The gateway refuses the call
without one and records it in cleartext on the call's audit row, the one
field of the ledger that is not a digest; `poseidon run story` reads it back
under the row. Every other tool's row carries a digest and nothing more.

The tables are rendered from the gateway's tool definitions in
`packages/mcp-gateway/src/mcp_gateway/app.py` and the catalog in
`packages/schema/src/schema/tools.py` at build time; a tool present in one
and not the other fails the build.

## Sandbox tools

All address the run's single sandbox, resolved from `run_id`. They are
*run-scoped* in policy (`bound_to_run`): the caller must be an ephemeral
agent verifiably bound to the very run it names.

<!-- gen: tools:sandbox -->

## Repository and pull-request tools

<!-- gen: tools:repository -->

## Issue tools

The ticket surface of the target repository: the PM files and comments
tickets when it scopes, and the platform files rework and bootstrap
tickets under its own identity.

<!-- gen: tools:issues -->

## Architecture requests

A crew member's one way to change the architecture masterplan: the
proposal is filed by the platform as a stewardship ticket on the company's
repository — the caller chooses neither the crew nor the repository — and
the steward revises the plan on a later tick. One request per run, bound
to the run like a sandbox call. Asking is not writing.

<!-- gen: tools:architecture -->

## Not tools: the admin routes

The gateway also serves two plain HTTP routes, `POST` and
`DELETE /admin/sandboxes`, behind the same identity middleware. They
register and retire a run's sandbox URL and are called by the platform
lifecycle during fulfilment and teardown. They are not part of the MCP
surface an agent can name.
