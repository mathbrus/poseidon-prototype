# MCP — the Model Context Protocol

MCP is a protocol for exposing *tools* to a language model: named
operations with typed arguments, which a model can call and whose results
come back into its context. Think of it as a function-calling ABI that is
transport-agnostic and language-agnostic.

Why it matters here: MCP gives a natural place to stand between the model
and its effects. The agent does not open files or run git — it calls
`sandbox.write_file` or `git.open_pr`, and something else decides whether
that call happens.

Poseidon's gateway is an MCP server; the agent is an MCP client. Everything
the agent can do to the world is one of the names on the
[Tools](../reference/tools.md) page, which is what makes "the tool surface
is the security surface" true rather than aspirational. Some of those
names belong to the platform lifecycle alone: `repo.archive`, how a
workspace enters a sandbox, and `git.merge_pr` with its two consequences,
`issues.close` and `git.close_pr` — none of which a blueprint can hand to
an agent role.

A detail worth noticing: the action schemas the model sees contain no
`run_id` and no `target_repo`. The agent host injects both at call time
(`packages/agent-host/src/agent_host/hands.py`), so the model cannot forge what it
cannot name. And because that injection happens inside the agent's own pod,
it is not trusted either: the gateway independently checks the claimed
`run_id` against the caller's registry binding, and a mismatch resolves to
*not bound* — denied. Two layers, with the outer one assuming the inner one
lied.
