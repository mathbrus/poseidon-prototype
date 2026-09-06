# Poseidon

**An autonomous company, run by cloud agents, that cannot
exceed its authority — because the infrastructure will not let it, not
because the prompt asked nicely.**

You give Poseidon a charter — a vision to pursue, a budget to stay under,
a short list of things that must never happen without you. It stands up a
small software organisation to pursue that vision: a steward that writes
the strategy and the architecture masterplan from it, a product manager
that scopes the next slice and files tickets, ephemeral crews that
implement and review each change, and a standing heartbeat that decides
what to do next and
keeps going. Nothing else goes in — no goal file, no task, no "please do
X". Real pull requests appear on your repository. You approve the merges
that matter and watch the rest happen. When you are ready to grant more, the
company does more of its own SDLC unattended — up to and including merging
its own passing work — and never one step past the authority you wrote down.

Plenty of tools let an agent open a pull request. What is hard, and what
Poseidon is about, is letting a *company of agents work toward a vision on
its own* while guaranteeing it cannot force-push to `main`, spend past its
budget, exfiltrate a secret, or act on an instruction hidden in a GitHub
issue — not because a careful prompt asked it not to, but because the
surrounding system makes those outcomes impossible and records every
attempt. That ambition sets the bar in five places:

- **Autonomy** — the system chooses the next action; a human declares the
  gates and the caps but does not stitch the phases together.
- **Continuity** — standing agents that remember across restarts.
- **Coordination** — many workstreams, shared artifacts, real priorities.
- **Quality** — the platform, never the agent, is the gate of record for
  "the tests pass," and the tests can be split off where the implementer
  cannot rewrite them.
- **Authority** — every consequence is decided by policy a human can read,
  diff and revoke. Injection is assumed; containment is positional; spend is
  a stop, not a warning.

> [!NOTE]
> **Status: working prototype.** Every capability is exercised by the
> end-to-end suite against a real Kubernetes cluster. It has run only on
> kind (local Kubernetes), never in production, and the documentation is
> deliberately specific about what it cannot do.

## The documentation

The docs site is the place to read the argument, follow a guide, or look
something up. Build it locally with `uv run --group docs mkdocs serve`, or
download the `docs-site` artifact of any CI run; its pages live under
[`docs/`](docs/).

- **The argument** — the problem, the core idea, the anatomy of a run, what
  it can and cannot do. Written to be read start to finish.
- **Concepts** — primers on MCP, OPA and Rego, workload identity,
  NetworkPolicy, Temporal, and standing vs. ephemeral agents.
- **Guides** — bring up a cluster, drive a run, hand over the heartbeat and
  the merge, run the acceptance suite.
- **Reference** — the blueprint, roles and crews, tools, the console and
  its CLI, the Temporal contract, policy and audit. Rendered from the code.

## Quickstart

It costs nothing: the default brain is a deterministic mock.

```bash
uv sync
for p in packages/*/; do uv run pytest "$p" -q; done   # thirteen suites, no cluster needed
cp deploy/kind/.env.example deploy/kind/.env            # fill in GIT_TOKEN and the suite's TARGET_REPO
deploy/kind/kind-up.sh                                  # cluster, images, chart, the company
export POSEIDON_TOKEN=$(uv run poseidon login owner)    # you, to the console
scripts/acceptance.sh                                   # the whole suite, zero LLM spend
```

## Packages

Each package has its own README with a module map, entry points, invariants
and how to test it.

| Package | What it owns |
|---|---|
| [`common`](packages/common/README.md) | Identity, workload authn, audit model, pod security posture |
| [`schema`](packages/schema/README.md) | The blueprint contract and the role/crew catalogs |
| [`compiler`](packages/compiler/README.md) | Blueprint → deployment plan, as a pure function |
| [`registry`](packages/registry/README.md) | Nine Postgres tables; one writer each; the audit store |
| [`operator`](packages/operator/README.md) | Reconciles the CRs; serves policy bundles |
| [`orchestrator`](packages/orchestrator/README.md) | Temporal workflows, ephemeral pods, gates |
| [`mcp-gateway`](packages/mcp-gateway/README.md) | The MCP tool chokepoint; the only holder of `GIT_TOKEN` |
| [`llm-gateway`](packages/llm-gateway/README.md) | The model chokepoint; metering and budget enforcement |
| [`console`](packages/console/README.md) | The human's chokepoint: verify the person, ask policy, act, audit |
| [`cli`](packages/cli/README.md) | `poseidon-cli`: the `poseidon` CLI and the console client it is built on |
| [`agent`](packages/agent/README.md) | The agent host: the dispatch API and the OpenHands SDK procedures behind it |
| [`sandbox`](packages/sandbox/README.md) | Backend contract, K8s Jobs backend, tool server, egress proxy |
| [`mockllm`](packages/mockllm/README.md) | The deterministic brain that makes e2e runs free |

---

<sub>Prototype. Read what it cannot do before trusting it with anything
that matters.</sub>
