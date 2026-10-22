# Poseidon

Poseidon runs a small software company of AI agents on Kubernetes. You
write the company's vision, budget and merge rules in a YAML file
called a [blueprint](reference/blueprint.md). The agents plan, build and
review the work on their own. Policy, network rules and gateways enforce
your rules, so a prompt-injected agent can't get past them.

!!! note "Status: prototype"
    Every capability on this page is tested end to end on a local
    [kind](https://kind.sigs.k8s.io/) cluster. Poseidon has never run in
    production. [Limitations](explanation/limitations.md) lists what it
    does not do yet.

## Where to start

| You want to… | Read |
|---|---|
| See a company run on your machine, for free | [Quickstart](get-started/quickstart.md) |
| Understand how the agents are organized and contained | [How it works](explanation/how-it-works.md), then the [security model](explanation/security-model.md) |
| Do one specific task | [How-to guides](how-to/delegate-merges.md) |
| Look up a field, command, tool or policy rule | [Reference](reference/blueprint.md) |
| Change Poseidon itself | [Contributing](developing/contributing.md) |

## What you get

The end-to-end acceptance suite tests every row of this table.

| Capability | What it means |
|---|---|
| **A company from one file** | You compile, preview, apply, edit and delete a whole company from one blueprint. `poseidon blueprint preview` shows every change before it happens. |
| **Work from a vision alone** | The company writes its own strategy and architecture plan from your vision, then turns them into tickets. You never hand it a task. |
| **The full development cycle** | Ticket → code in a sandbox → tests run by the platform → pull request → independent review → merge. |
| **Merges you control** | By default, you approve every merge. You can let policy merge changes in the paths you choose, but only when the review passed and the platform's own test run is green. |
| **Tests the implementer can't rewrite** | A separate crew can write the acceptance tests, and only you merge them. Delegated merges are graded against those tests. |
| **Unattended work within caps** | The company starts its own work within your caps on concurrent runs, open tickets and daily spend. `poseidon pause` holds it. |
| **Budgets that stop spending** | When a run reaches its budget, the LLM gateway refuses further model calls and the run waits for you. Every dollar is attributed to a run and an agent. |
| **Contained agents** | Agent pods hold no credentials. Sandboxes can't reach the cluster API and have one audited route to the internet. Policy checks every call. |
| **Durable runs** | If the orchestrator dies mid-run, the run resumes where it left off, and nothing leaks. |
| **One audit trail** | Every component writes to one audit table. One run id returns the run's whole story. |
