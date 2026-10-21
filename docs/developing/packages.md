# Packages

Poseidon is thirteen `uv` workspace packages under `packages/`. Each has a
README next to its code with a module map, entry points, invariants and
how to test it.

| Package | What it owns |
|---|---|
| [`common`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/common/README.md) | Identity, workload authentication, the audit model, pod security settings, and the names every package shares |
| [`blueprint`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/blueprint/README.md) | The blueprint's schema and checks, and the role, crew and tool catalogs |
| [`compiler`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/compiler/README.md) | Blueprint to deployment plan and policy bundle, as a pure function |
| [`registry`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/registry/README.md) | The Postgres tables every service reads and writes, and the audit store |
| [`reconciler`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/reconciler/README.md) | Turns the `Company` and `AgentInstance` resources into a running company; serves policy bundles |
| [`orchestrator`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/orchestrator/README.md) | The Temporal workflows, ephemeral agents and sandboxes, and the gates |
| [`mcp-gateway`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/mcp-gateway/README.md) | Every tool call; the only holder of the GitHub token |
| [`llm-gateway`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/llm-gateway/README.md) | Every model call: attribution, budget caps, metering |
| [`console`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/console/README.md) | Every act of a person: verify, ask policy, record, act |
| [`cli`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/cli/README.md) | The `poseidon` CLI, a client of the console |
| [`agent-host`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/agent-host/README.md) | The process in every agent pod: the dispatch API and the procedures it runs |
| [`sandbox`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/sandbox/README.md) | The sandbox backend, its tool server, and the egress proxy |
| [`mock-brain`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/mock-brain/README.md) | The deterministic brain that makes test runs free |

[Architecture](../explanation/architecture.md) shows which service holds
which secret, and how they fit together.
