# Blueprint

A blueprint is the YAML file that defines a company. The console validates
it against a JSON Schema, compiles it, and applies it. This page lists
every field. The tables are rendered from the schema in
`packages/blueprint/src/blueprint/schema.py`.

- Unknown keys are errors, so a typo never goes unnoticed.
- No field has a default. Every cap and bound you rely on is written in
  the file.
- To write your first blueprint, follow [Write your blueprint](../how-to/write-a-blueprint.md).
  It also lists the errors people meet most often.

## Example

The shipped example, `examples/hello-saas.yaml`. Every merge waits for a
person:

```yaml
--8<-- "examples/hello-saas.yaml"
```

## Sections

<!-- gen: blueprint-sections -->

## `apiVersion`

<!-- gen: blueprint-fields:apiVersion -->

## `company`

The company's identity and the people who may act on it. The vision is
the only instruction the company gets. Applying the blueprint again is
the only way to change it.

<!-- gen: blueprint-fields:company -->

## `stack`

The repository every run works on, and the sandbox its code runs in.
`egress_allow` is the complete list of hosts a sandbox may reach. An empty
list means no egress at all.

<!-- gen: blueprint-fields:stack -->

## `standing`

The agents that live as long as the company. A standing agent must be a
conductor, an agent that may start crews. Agents that write code are always
ephemeral ([why](../concepts/standing-vs-ephemeral.md)).

<!-- gen: blueprint-fields:standing -->

## `tools`

Narrows a role's tools, for every agent of that role. A role not listed
keeps its catalog tools ([Roles and crews](roles.md)). A scope can narrow
the catalog, never widen it.

<!-- gen: blueprint-fields:tools -->

## `workflows`

Which crew does each kind of run, and the gates a run parks on. Who decides
what runs, and when, is the [`heartbeat`](#heartbeat) section's job.

The merge gate is [explained in full here](../explanation/merge-gate.md).
To let policy merge in chosen paths, see
[Automate merges](../how-to/automate-merges.md):

```yaml
--8<-- "examples/hello-saas-automated.yaml:review-automated"
```

<!-- gen: blueprint-fields:workflows -->

## `policies`

Rules on what a delivery may change. The gateway checks them against the
real changed paths before it pushes. If it can't compute the changed paths,
it denies the delivery.

`contract_tests` declares acceptance tests the implementer can't rewrite
([how they work](../explanation/merge-gate.md#tests-the-implementer-cant-rewrite)):

```yaml
--8<-- "examples/hello-saas-automated.yaml:contract_tests"
```

<!-- gen: blueprint-fields:policies -->

### What the agents are told

Every agent gets the rules that bind its work, rendered from the blueprint
into its system prompt. They are called the *bounds*. For
`examples/hello-saas.yaml` they read:

<!-- gen: bounds:hello-saas -->

When the gateway denies a call, the denial names the rule and what it
refused, such as the branch and the pattern it had to match.

## `budgets`

The cap on what one run may spend on model calls. At the cap, the LLM
gateway refuses further calls and the run parks in `parked-budget_exhausted`
until a person answers ([how](../how-to/gates-and-runs.md)).

<!-- gen: blueprint-fields:budgets -->

## `heartbeat`

Who decides what the company does next, and the caps on what it may start.
Every company has one. [How the heartbeat decides](../explanation/heartbeat.md)
explains each cap.

<!-- gen: blueprint-fields:heartbeat -->
