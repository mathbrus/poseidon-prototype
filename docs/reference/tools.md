# Tools

The MCP gateway exposes <!-- gen: tool-count --> tools. They are everything
an agent, or the platform itself, can do to a sandbox, the repository or
its issues. For every call, the gateway verifies the caller, asks policy,
and records one audit event. A denied call is refused before anything
happens.

- **`run_id` and `repo` are never chosen by the model.** The agent host
  adds them to every call. The gateway checks the run against the caller's
  identity, and policy denies any repository but `stack.repo.url`.
- **Who may call what is compiled policy.** The last column lists the
  roles whose catalog entry holds the tool, before a blueprint narrows it.
  A tool no role holds belongs to the platform alone.
- **Five tools take a `rationale`:** `issues.create`, `git.open_pr`,
  `architecture.request`, `issues.close` and `git.close_pr`. Each commits
  the company to something, so the caller must say why. The reason is
  stored in cleartext, and `poseidon run story` shows it.

Each description below is the first sentence of the tool's docstring in
`packages/mcp-gateway/src/mcp_gateway/app.py`. The full docstring is what
the agents read.

## Sandbox tools

Each addresses the run's one sandbox. Policy allows them only to an
ephemeral agent bound to the run it names (`bound_to_run`).

<!-- gen: tools:sandbox -->

## Repository and pull-request tools

<!-- gen: tools:repository -->

## Issue tools

The PM files tickets and comments on them. The platform files rework and
bootstrap tickets under its own identity. Only the platform closes a
ticket, and only when a pull request that fulfills it merges, whether
the company merged it or a person did.

<!-- gen: tools:issues -->

## Architecture requests

An ephemeral agent's one way to propose a change to the architecture
masterplan. The platform files the proposal as a stewardship ticket, and
the steward revises the plan on a later tick. One request per run.

<!-- gen: tools:architecture -->

## Admin routes

The gateway also serves `POST` and `DELETE /admin/sandboxes`, behind the
same identity check. The platform calls them to register and retire a
run's sandbox. They are not MCP tools, and no agent can name them.
