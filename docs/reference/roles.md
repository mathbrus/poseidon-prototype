# Roles and crews

A **role** is what an agent *is*: its tool ceiling, the crews it may
spawn, and the procedures a dispatch may ask of it. A **crew** is the list
of roles fulfilled together for one run; each becomes one ephemeral agent
with its own pod and identity, under the conductor's authority. Both
catalogs are closed: a blueprint names roles and crews, it never defines
them, because a role a blueprint could invent would be a policy the
platform never reviewed.

The tables on this page are rendered from `packages/schema/src/schema/catalog.py`
at build time.

## Roles

| Role | What it is | Procedures | May spawn | Tools |
|---|---|---|---|---|
| `pm` | Standing conductor: decides the company's next action on every heartbeat tick, scopes the next slice of work into tickets, and is the authority every crew is fulfilled under. Holds no coding tools — the PM cannot open a pull request or write in a sandbox. | `scope`, `decide` | `contract-authoring`, `feature-delivery`, `feature-review`, `stewardship` | `issues.create`, `issues.read`, `issues.comment` |
| `implementer` | Implements a scoped task in the run's sandbox and delivers it as a pull request. Ephemeral agents only; standing counts are a compile error. | `implement`, `deliver` | — | `sandbox.read_file`, `sandbox.list_dir`, `sandbox.exec`, `sandbox.run_tests`, `sandbox.write_file`, `sandbox.edit_file`, `sandbox.git_bundle`, `architecture.request`, `git.open_pr` |
| `reviewer` | Reads the diff, runs the tests, posts findings, gives a verdict. | `review` | — | `sandbox.read_file`, `sandbox.list_dir`, `sandbox.exec`, `sandbox.run_tests`, `sandbox.write_file`, `sandbox.edit_file`, `sandbox.git_bundle`, `git.read`, `git.comment`, `architecture.request`, `issues.comment` |
| `contract-author` | Writes the contract/acceptance tests a slice is graded by. Ephemeral agents only, like the implementer. | `implement`, `deliver` | — | `sandbox.read_file`, `sandbox.list_dir`, `sandbox.exec`, `sandbox.run_tests`, `sandbox.write_file`, `sandbox.edit_file`, `sandbox.git_bundle`, `architecture.request`, `git.open_pr` |
| `steward` | Writes the company's strategy once and its architecture masterplan at bootstrap and on request. Ephemeral agents only, like the implementer. | `implement`, `deliver` | — | `sandbox.read_file`, `sandbox.list_dir`, `sandbox.exec`, `sandbox.run_tests`, `sandbox.write_file`, `sandbox.edit_file`, `sandbox.git_bundle`, `git.open_pr` |

A blueprint may **narrow** a role's tool list in its `tools.<role>` section,
never widen it, and the scope applies to every agent of the role, standing
or ephemeral. The lean-standing rule is not in the table because it is the
compiler's: `implementer`, `reviewer`, `contract-author` and `steward` exist
only as ephemeral agents, and a blueprint that hires one standing is a
compile error. The `steward` is the one writer of the company's documents;
a future standing **observer** would revise the strategy, and is not in
the catalog.

## Which role runs which procedure

A **procedure** is the agent host's unit of work. A dispatch names one; the
host refuses any the role does not declare before a model call is made.

| Procedure | Run by |
|---|---|
| `scope` | `pm` |
| `decide` | `pm` |
| `implement` | `implementer`, `contract-author`, `steward` |
| `deliver` | `implementer`, `contract-author`, `steward` |
| `review` | `reviewer` |

## Crews

| Crew | What it is | Roles |
|---|---|---|
| `feature-delivery` | One implementer for an implement run. | `implementer` |
| `feature-review` | One reviewer for a review run, against a sandbox from the PR ref. | `reviewer` |
| `contract-authoring` | One contract author delivering the acceptance tests for a slice. | `contract-author` |
| `stewardship` | One steward delivering a company document. | `steward` |

A blueprint names the crews it puts to work in a hire's `may_spawn` and a
workflow's `crew`, never their members. Spawn authority is authority to
create a principal from a compiler-vetted role, not a capability ceiling:
the PM cannot open a pull request, yet the crew it spawns can.
