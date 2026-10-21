# Packages

Thirteen `uv` workspace packages under `packages/`. Each has its own README
next to the code — a module map, entry points, invariants and how to test
it — written for the contributor who has the package open. Those READMEs
are not mirrored here; the table links to them.

| Package | What it owns |
|---|---|
| [`common`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/common/README.md) | Identity, workload authn, audit model, pod security posture |
| [`blueprint`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/blueprint/README.md) | The blueprint contract and the role/crew catalogs |
| [`compiler`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/compiler/README.md) | Blueprint → deployment plan, as a pure function |
| [`registry`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/registry/README.md) | Nine Postgres tables; one writer each; the audit store |
| [`reconciler`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/reconciler/README.md) | Reconciles the CRs; serves policy bundles |
| [`orchestrator`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/orchestrator/README.md) | Temporal workflows, ephemeral pods, gates |
| [`mcp-gateway`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/mcp-gateway/README.md) | The MCP tool chokepoint; the only holder of `GIT_TOKEN` |
| [`llm-gateway`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/llm-gateway/README.md) | The model chokepoint; metering and budget enforcement |
| [`console`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/console/README.md) | The human's chokepoint: verify the person, ask policy, act, audit |
| [`cli`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/cli/README.md) | `poseidon-cli`: the `poseidon` CLI and the console client it is built on |
| [`agent-host`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/agent-host/README.md) | The agent host: the dispatch API and the OpenHands SDK procedures behind it |
| [`sandbox`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/sandbox/README.md) | Backend contract, K8s Jobs backend, tool server, egress proxy |
| [`mock-brain`](https://github.com/mathbrus/poseidon-prototype/blob/master/packages/mock-brain/README.md) | The deterministic brain that makes e2e runs free |

Which service holds which secret, and what each is responsible for, is on
[Architecture](../explanation/architecture.md).
