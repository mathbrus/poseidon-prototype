# Tools

The MCP gateway exposes 17 tools. They are the entire
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

The tables are rendered from the gateway's tool definitions in
`packages/mcp-gateway/src/mcp_gateway/app.py` and the catalog in
`packages/schema/src/schema/tools.py` at build time; a tool present in one
and not the other fails the build.

## Sandbox tools

All address the run's single sandbox, resolved from `run_id`. They are
*run-scoped* in policy (`bound_to_run`): the caller must be an ephemeral
agent verifiably bound to the very run it names.

| Tool | Arguments | What it does | Catalog roles holding it |
|---|---|---|---|
| `sandbox.read_file` | `run_id, path, view_range?` | One file from the run's workspace — all of it, or the 1-based inclusive `[start, end]` line range (`-1` for end of file) — with `start_line` and `total_lines`. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.write_file` | `run_id, path, content` | Create or overwrite one file in the run's workspace. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.edit_file` | `run_id, path, command, new_str, old_str?, insert_line?` | Partial edit of an existing file: `str_replace` swaps text that occurs exactly once (ambiguous or absent text is a backend error, the file untouched); `insert` adds lines after `insert_line` (`0` = top). Returns a numbered snippet around the change. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.list_dir` | `run_id, path` | List a workspace directory. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.exec` | `run_id, cmd, timeout_s=60` | Run a shell command in the sandbox and return its output and exit code. The command runs with no credentials, no cluster API, and the egress proxy as its only route out. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.run_tests` | `run_id, paths?` | Run the blueprint's `test_command` and return the result. Agents may call it to check their work; the verify gate of record is the orchestrator's own separate invocation. With `paths`, the run is scoped to those test paths — the platform's contract-verify call, where collecting nothing from a named path is a red exit, not a silent pass. | `implementer`, `reviewer`, `contract-author`, `steward` |
| `sandbox.git_bundle` | `run_id, branch` | A git bundle of the named branch's commits since the recorded base ref, base64-encoded. How work leaves the sandbox: as verifiable content, not as a push. | `implementer`, `reviewer`, `contract-author`, `steward` |

## Repository and pull-request tools

| Tool | Arguments | What it does | Catalog roles holding it |
|---|---|---|---|
| `git.open_pr` | `run_id, repo, branch, title, body, bundle_b64` | The only path by which code leaves the system. The gateway verifies the bundle, applies it to a fresh platform-side clone, extracts the changed paths — and which of them already exist on the base ref — and feeds them to policy as real arguments (branch protection, `deny_paths`, the contract-tree owner rule, the document fences and the write-once strategy), then pushes the branch with its own token and opens the pull request. | `implementer`, `contract-author`, `steward` |
| `git.read` | `run_id, repo, number` | Read a pull request — metadata and diff — for review. | `reviewer` |
| `git.comment` | `run_id, repo, number, body` | Post a comment on the pull request's conversation; how the reviewer publishes findings. | `reviewer` |
| `repo.archive` | `run_id, repo, ref, overlay_ref?, overlay_paths?` | Clone `repo` at `ref` with the platform token, strip the remote (and with it the credentialed URL), and return the workspace as a base64 tarball. With `overlay_paths`, those paths are mirrored from `overlay_ref` over the checkout — removed first, then re-checked-out — which is how the platform composes the contract-verify tree. Platform lifecycle only: how code enters a sandbox during fulfilment. | — |
| `git.merge_pr` | `run_id, repo, number, method='merge'` | Merge the pull request. Compiled policy grants this to the platform identity alone, and the orchestrator calls it only after the merge gate resolved to merge. No blueprint can hand it to an agent role. | — |
| `repo.read_file` | `run_id, repo, ref, path` | One file of the repository at `ref`, as the git host holds it — `{present, text}`, a missing file being an ordinary answer. Platform lifecycle only; the orchestrator reads the company's documents with it at `base_ref`, never from a sandbox a run could have edited. | — |

## Issue tools

The ticket surface of the target repository: the PM files and comments
tickets when it scopes, and the platform files rework and bootstrap
tickets under its own identity.

| Tool | Arguments | What it does | Catalog roles holding it |
|---|---|---|---|
| `issues.create` | `run_id, repo, title, body` | File a new issue — how the PM turns the next slice of the strategy into tickets, and how the platform files rework and bootstrap tickets. | `pm` |
| `issues.read` | `run_id, repo, number?` | Read one issue by number (refusing pull requests — those belong to `git.read`), or the open backlog when `number` is omitted. An issue-driven implement run resolves its ticket through this tool under the platform identity. | `pm` |
| `issues.comment` | `run_id, repo, number, body` | Comment on an issue — how the PM leaves its one-paragraph scoping summary on each ticket it files. | `pm`, `reviewer` |

## Architecture requests

A crew member's one way to change the architecture masterplan: the
proposal is filed by the platform as a stewardship ticket on the company's
repository — the caller chooses neither the crew nor the repository — and
the steward revises the plan on a later tick. One request per run, bound
to the run like a sandbox call. Asking is not writing.

| Tool | Arguments | What it does | Catalog roles holding it |
|---|---|---|---|
| `architecture.request` | `run_id, rationale, proposal` | Ask for a change to the architecture masterplan. The platform files the proposal as a stewardship ticket on the company's repository — the caller chooses neither the crew nor the repository — and the steward revises the plan on a later tick. One request per run; asking is not writing. | `implementer`, `reviewer`, `contract-author` |

## Not tools: the admin routes

The gateway also serves two plain HTTP routes, `POST` and
`DELETE /admin/sandboxes`, behind the same identity middleware. They
register and retire a run's sandbox URL and are called by the platform
lifecycle during fulfilment and teardown. They are not part of the MCP
surface an agent can name.
