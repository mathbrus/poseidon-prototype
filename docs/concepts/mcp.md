# MCP: the Model Context Protocol

[MCP](https://modelcontextprotocol.io/) is a protocol for giving a
language model *tools*: named operations with typed arguments, which the
model calls and whose results come back into its context. Think of it as
function calling that works across languages and transports.

## Why Poseidon uses it

MCP puts something between the model and its effects. An agent doesn't
open files or run git. It calls `sandbox.write_file` or `git.open_pr`, and
the MCP gateway decides whether that call happens.

The gateway is an MCP server, and each agent is an MCP client. Everything
an agent can do to the world is one of the names on the
[Tools](../reference/tools.md) page. If a tool isn't on that list, no agent
can do it.

## What the model can't name

The tool schemas the model sees have no `run_id` and no `repo` argument.
The agent host adds both when it makes the call, so the model can't aim a
call at another run or another repository.

The agent host runs in the agent's own pod, so the gateway doesn't trust
it either. It checks the claimed `run_id` against the caller's identity
in the registry, and denies the call if they don't match.
