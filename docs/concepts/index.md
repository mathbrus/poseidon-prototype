# Concepts

Short primers on the technologies Poseidon is built on. Each page
explains one idea and why Poseidon needs it. Skip any you already know.

| Concept | Why Poseidon needs it |
|---|---|
| [MCP](mcp.md) | Every agent action is a named tool call, so a gateway can decide each one. |
| [OPA and Rego](opa.md) | Policy is compiled from your blueprint and answers every "may this caller do this?" |
| [Workload identity](workload-identity.md) | The gateways know which agent is calling without any shared password. |
| [NetworkPolicy](network-policy.md) | Pods that shouldn't talk can't, even if every other layer fails. |
| [Temporal](temporal.md) | Runs survive crashes, and cleanup always happens. |
| [Standing vs. ephemeral agents](standing-vs-ephemeral.md) | Agents that write code exist for one run only. |

The [glossary](../reference/glossary.md) defines Poseidon's own terms.
