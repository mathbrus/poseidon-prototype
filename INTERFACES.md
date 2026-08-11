# Interfaces

Poseidon has exactly two ways in, and they are deliberately different in
kind. **Humans** operate the platform through the `poseidon` CLI: they
start runs, deliver gate verdicts, and apply blueprints. **Agents** act on
the world through the MCP tools exposed by the gateway — and through
nothing else. This document describes both surfaces, function by function.

Everything here is generated reading the source: the CLI lives in
`packages/orchestrator/src/orchestrator/cli/`, the tool definitions in
`packages/gateway/src/gateway/app.py`. If this document and the code ever
disagree, the code wins — and the document has a bug.

---

## 1. The `poseidon` CLI

The CLI is a **thin Temporal client**. It connects to the Temporal
frontend (`TEMPORAL_ADDRESS`, default `127.0.0.1:7233` — on kind, reach it
with `kubectl -n poseidon-platform port-forward svc/temporal-frontend
7233:7233`) and starts, signals, or inspects workflows. It holds no
credentials and performs no work itself: every run executes on the
in-cluster worker, under the platform's identity, regardless of who
invoked the CLI or from where.

Workflow ids are `{workflow}-{run_id}` across all three workflow families,
which is why several commands take a `--workflow` option: the CLI needs to
know which family a run belongs to in order to address it.

### `poseidon run`

```
poseidon run TASK_FILE [--run-id ID] [--workflow implement|intake]
             [--detach] [--fast-timeouts]
```

Starts a run for the task or goal described in `TASK_FILE`.

- `--workflow implement` (the default) treats the file as a **task** and
  starts the full delivery pipeline: crew fulfilment, planning,
  implementation, the platform-run verify gate, and PR delivery.
- `--workflow intake` treats the file as a **goal** and dispatches the PM
  to scope it into tickets and a recorded crew request.
- `--run-id` fixes the run id (one is generated when omitted).
- `--detach` starts the workflow and exits immediately. Without it, the
  CLI blocks until the run completes, prints the run record as JSON, and
  exits non-zero if the final status is not `done`.
- `--fast-timeouts` swaps in short per-activity bounds sized for the mock
  brain, so a wedged step fails in seconds instead of minutes. Used by the
  e2e suite; not meant for real-model runs.

### `poseidon review`

```
poseidon review PR_NUMBER [--run-id ID] [--detach] [--fast-timeouts]
```

Starts a review run for an open pull request: a fresh sandbox is
provisioned from the PR's head ref and an ephemeral reviewer (read-and-
judge tools only) runs the tests and posts findings. The workflow then
parks on the **merge gate** until a human answers via `poseidon signal` —
so `--detach` is the usual mode here, since the gate may hold for hours.

### `poseidon signal`

```
poseidon signal RUN_ID NAME [--approved/--no-approved] [--payload TEXT]
                [--workflow implement|intake|review]
```

Sends a gate signal to a run's workflow — this is the human-in-the-loop
verdict path. `NAME` is the gate (e.g. `merge`), `--approved` carries the
verdict (**default: approved**, so an explicit `--no-approved` is how you
reject), and `--payload` attaches optional free text. The canonical use:

```
poseidon signal <run_id> merge --workflow review          # approve
poseidon signal <run_id> merge --no-approved --workflow review   # reject
```

On approval, the *orchestrator* performs the merge under its platform
identity; the CLI never touches the repository.

### `poseidon status`

```
poseidon status RUN_ID [--workflow implement|intake|review]
```

Prints the workflow's status as JSON, and — when the workflow has
completed — the full run record alongside it.

### `poseidon blueprint compile`

```
poseidon blueprint compile BLUEPRINT [--out DIR] [--live]
```

Compiles the blueprint and prints the deployment plan as JSON, without
touching the cluster. `--out` additionally writes the emitted Rego bundle
to a directory, one file per rule set — useful for inspecting exactly what
policy a blueprint produces. `--live` diffs against the live registry;
without it the plan is compiled against nothing, which is what CI wants.

### `poseidon blueprint preview`

```
poseidon blueprint preview BLUEPRINT [--live/--no-live]
```

Shows what applying the blueprint would change — instances hired and
fired, in diff form — plus the budget line: the per-run cap and the
projected monthly cost. Live diffing is the default here, since a preview
against nothing answers no interesting question.

### `poseidon blueprint apply`

```
poseidon blueprint apply BLUEPRINT [--yes]
```

Compiles the blueprint and submits the Company and AgentInstance custom
resources; the operator then converges the cluster to match. Without
`--yes` the command shows the preview diff and asks for confirmation
first — firing an agent is a deletion in that diff, and deleting its
ServiceAccount is what revokes its identity.

---

## 2. The gateway's MCP tools

The MCP gateway exposes **16 tools**. They are the entire action surface:
everything any agent — or the platform itself — does to a sandbox, the git
host, or the issue tracker is one of these names. Every call follows the
same pipeline: authenticate the caller's workload identity, build the
policy input, ask OPA, write an audit event, and only then act.

Three things to keep in mind while reading the signatures:

- **`run_id` appears in every signature but no model ever writes it.** The
  action schemas the model sees are `run_id`-free; the brain host injects
  it at call time, and the gateway independently verifies the claimed run
  against the caller's registry binding. A mismatch is a denial.
- **`repo` is likewise injected** (as `target_repo`) by the brain host,
  never chosen by the model.
- **Who may call what is compiled policy**, not a property of the tool.
  The "typical caller" notes below describe the shipped `hello-saas`
  blueprint, where the executor holds the sandbox tools plus `git.open_pr`,
  the planner reads only, the reviewer reads and comments, the PM handles
  issues and crew requests, and `git.merge_pr` and `repo.archive` belong
  to the platform lifecycle alone.

### Sandbox tools

All six address the run's single sandbox, resolved from `run_id`. They are
*run-scoped* in policy (`bound_to_run`): the caller must be a verified
participant of the very run it names.

| Tool | Arguments | What it does |
|---|---|---|
| `sandbox.read_file` | `run_id, path` | Returns the contents of one file in the run's workspace. |
| `sandbox.write_file` | `run_id, path, content` | Creates or overwrites one file in the workspace. |
| `sandbox.list_dir` | `run_id, path` | Lists a workspace directory. |
| `sandbox.exec` | `run_id, cmd, timeout_s=60` | Runs a shell command in the sandbox and returns its output and exit code. The command runs with no credentials, no cluster API, and the egress proxy as its only route out. |
| `sandbox.run_tests` | `run_id` | Runs the blueprint's `test_command` (the sandbox's `TEST_CMD`) and returns the result. Agents may call it to check their work; the verify gate of record is the orchestrator's own separate invocation. |
| `sandbox.git_bundle` | `run_id, branch` | Produces a git bundle of the named branch's commits since the recorded base ref, base64-encoded. This is how work leaves the sandbox: as verifiable content, not as a push. |

Typical callers: the executor (all six), the planner (`read_file`,
`list_dir` only), the reviewer (reads plus `exec`/`run_tests`).

### Repository and pull-request tools

| Tool | Arguments | What it does |
|---|---|---|
| `repo.archive` | `run_id, repo, ref` | Clones `repo` at `ref` with the platform token, strips the remote (and with it the credentialed URL), and returns the workspace as a base64 tarball. Platform lifecycle only: this is how code enters a sandbox during fulfilment. |
| `git.open_pr` | `run_id, repo, branch, title, body, bundle_b64` | The only path by which code leaves the system. The gateway verifies the bundle, applies it to a fresh platform-side clone, extracts the changed paths and feeds them to policy as real arguments (branch protection, `deny_paths`, and path-ownership rules are checked here), then pushes the branch with its own token and opens the PR. |
| `git.read` | `run_id, repo, number` | Reads a pull request — metadata and diff — for review. |
| `git.comment` | `run_id, repo, number, body` | Posts a comment on the pull request's conversation; how the reviewer publishes findings. |
| `git.merge_pr` | `run_id, repo, number, method="merge"` | Merges the pull request. Compiled policy grants this to the platform identity alone, and the orchestrator calls it only after the human merge gate approves. No blueprint can hand it to an agent role. |

Typical callers: the executor (`git.open_pr`), the reviewer (`git.read`,
`git.comment`), the orchestrator (`repo.archive`, `git.merge_pr`).

### Issue tools

The ticket surface of the target repository, used chiefly by the PM during
intake.

| Tool | Arguments | What it does |
|---|---|---|
| `issues.create` | `run_id, repo, title, body` | Files a new issue — how a goal becomes tickets. |
| `issues.read` | `run_id, repo, number=None` | Reads one issue by number, or the open backlog when `number` is omitted. |
| `issues.update` | `run_id, repo, number, title=None, body=None, state=None` | Edits an issue's title, body, or open/closed state; omitted fields are left untouched. |
| `issues.comment` | `run_id, repo, number, body` | Comments on an issue. |

### Crew tools

| Tool | Arguments | What it does |
|---|---|---|
| `crew.request` | `run_id, crew_template, reason` | Records a conductor's intent to spawn a crew. An allowed request is *authorization plus intent, never fulfilment* — the orchestrator's implement workflow consumes the recorded row and does the provisioning. The compiled rule narrows `crew_template` to the caller's declared `may_spawn` list and enforces its `max_concurrent` cap against a live spawn count. |

### Not tools: the admin routes

For completeness — the gateway also serves two plain HTTP routes, `POST`
and `DELETE /admin/sandboxes`, behind the same identity middleware. They
register and retire a run's sandbox URL and are called by the platform
lifecycle during fulfilment and teardown. They are not part of the MCP
surface an agent can name.

---

<sub>Companion to the [root README](README.md); section 3 there teaches
the concepts (MCP, OPA, workload identity) that this reference assumes.</sub>
