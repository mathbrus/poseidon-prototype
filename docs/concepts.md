# Concepts

Short primers on the six things the [root README](../README.md) assumes: MCP,
OPA and Rego, workload identity, NetworkPolicy, Temporal, and the standing/
ephemeral split. Each is self-contained; skip any you already know.

<details>
<summary><b>MCP — the Model Context Protocol</b></summary>

<br>

MCP is a protocol for exposing *tools* to a language model: named
operations with typed arguments, which a model can call and whose results
come back into its context. Think of it as a function-calling ABI that is
transport-agnostic and language-agnostic.

Why it matters here: MCP gives a natural place to stand between the model
and its effects. The agent does not open files or run git — it calls
`sandbox.write_file` or `git.open_pr`, and something else decides whether
that call happens.

Poseidon's gateway is an MCP server exposing 17 tools. The agent is an MCP
client. Everything the agent can do to the world is one of those 16 names,
which is what makes "the tool surface is the security surface" true rather
than aspirational. (Sixteen of the seventeen are for agents. The seventeenth,
`repo.archive`, belongs to the platform lifecycle — no agent role declares
it, and the compiled bundle grants it to the platform identity alone. It is
how a workspace enters a sandbox, and it reappears in
[section 5](../README.md#5-anatomy-of-a-run).)

A detail worth noticing: the action schemas the model sees contain no
`run_id` and no `target_repo`. The brain host injects both at call time
(`packages/agent/src/agent/hands.py`), so the model cannot forge what it
cannot name. And because that injection happens inside the agent's own pod,
it is not trusted either: the gateway independently checks the claimed
`run_id` against the caller's registry binding, and a mismatch resolves to
*not bound* — denied. Two layers, with the outer one assuming the inner one
lied.

</details>

<details>
<summary><b>OPA and Rego — policy as a pure function</b></summary>

<br>

[Open Policy Agent](https://www.openpolicyagent.org/) is a general-purpose
policy engine. You give it a document of rules written in a language called
**Rego**, then ask it questions of the form "given this input, what is the
decision?"

The key property is that a decision is a *pure function of the policy bundle
and the input*. OPA reads no live data of its own. That makes decisions
reproducible and restart-safe: you can replay an input and get the same
answer.

A compiled Poseidon rule looks like this:

```rego
allow_rules contains "crew_feature_delivery_implementer_sandbox_exec" if {
	input.lifecycle == "ephemeral"
	input.company == "hello-saas"
	input.crew_template == "feature-delivery"
	input.role == "implementer"
	input.tool == "sandbox.exec"
	bound_to_run
}
```

Read it as: *an ephemeral caller of hello-saas, acting as the implementer of a
feature-delivery crew, may call `sandbox.exec` — provided its run binding
was verified.* Everything is default-deny; the lowest-sorting deny wins over
any allow.

That final clause, `bound_to_run`, deserves a second look. Sandbox tools
are *run-scoped*: the rule demands not just the right role but a verified
binding between this caller and the specific run whose sandbox the call
addresses. Without it, a role allow-list would quietly permit cross-run
access — any executor could read any run's workspace. Which tools carry
the requirement is a property of the tool, not of its name prefix
(`packages/schema/src/schema/tools.py` maintains the list); `git.open_pr`,
for instance, does not, because a pull request targets the repository
rather than a run's sandbox — its guards are the branch-protection and
changed-path rules you will meet in [the blueprint reference](blueprint.md).

**Nobody writes these by hand.** They are emitted by the compiler from the
blueprint, which is why a scope you did not declare cannot exist.

</details>

<details>
<summary><b>Workload identity — proving who is calling</b></summary>

<br>

A shared secret in an environment variable answers "does the caller know the
password?", which is not the same question as "who is the caller?" Anything
that reads the variable becomes the caller.

Kubernetes offers something better. The kubelet can mount a short-lived,
**audience-scoped** token for a pod's ServiceAccount, and rotate it. A
service receiving that token calls the **TokenReview** API to ask the
cluster who it belongs to. The answer is authoritative and the token is
useless anywhere outside its audience.

Poseidon uses exactly one identity shape: every agent pod — standing or
ephemeral — mounts a projected token for its own ServiceAccount, scoped
to the gateway's audience. What differs is which registry table the
gateway resolves the ServiceAccount against:

- **Standing agents** map through `agent_instances`, the standing agents the
  operator reconciles. The gateway resolves the ServiceAccount name to
  the row and — importantly — requires the recorded namespace to *match*.
  The same SA name in another namespace is an impersonation attempt, and
  is refused and audited.
- **Ephemeral agents** get a per-run ServiceAccount created at
  provision time, mapping through a live `run_participants` row — the
  row *is* the identity: run binding, crew coordinates, company, catalog
  template, namespace. A verified token alone is never enough; deleting
  the row revokes the identity **immediately**, however long the token
  itself remains valid.

That last point is the one worth internalising. The TokenReview may be
cached, but the registry row is re-read on **every call**: revocation as
a database write is far stronger than revocation as a short expiry — and
it works the same for firing a standing agent (its row is deleted with
its ServiceAccount) as for sweeping a crew.

</details>

<details>
<summary><b>NetworkPolicy — the silent backstop</b></summary>

<br>

By default every pod in a Kubernetes cluster can reach every other pod.
NetworkPolicy changes that: it is a firewall expressed in terms of pod
labels and namespaces rather than IP addresses.

Poseidon starts from deny-all in each company namespace and punches
specific holes. A sandbox may receive connections from the MCP gateway, and
may make connections to the egress proxy. That is the entire list.

Two things follow that are easy to miss:

1. **It needs a CNI that actually enforces it.** kind's default network
   plugin silently ignores NetworkPolicy, which would make every containment
   test pass for the wrong reason. The bootstrap installs Calico instead.
2. **It drops packets silently**, which makes it a poor *audit* layer. That
   is precisely why the egress proxy exists on top: NetworkPolicy is the
   backstop, and the proxy is what makes a blocked attempt *visible*.

</details>

<details>
<summary><b>Temporal — durable execution</b></summary>

<br>

A long-running agent process that dies takes its state with it. Retrying is
not obviously safe, because you do not know what already happened.

[Temporal](https://temporal.io/) inverts this. You write a *workflow* as
ordinary code, but the engine records every step and its result. If the
worker process dies, the workflow is replayed on a new worker from the
recorded history and continues from where it was. Side effects live in
*activities*, which are retried with policies you configure.

For Poseidon this buys two things that matter more than convenience:

- **Guaranteed compensation.** Teardown — revoking identities, destroying
  sandboxes, retiring registrations — runs on success, failure, cancel,
  timeout *and* worker death. There is one anchor for cleanup, and it always
  runs.
- **Human gates that survive restarts.** The merge gate is a workflow
  parked on a signal, possibly for hours. That is durable state, not a
  process holding a socket open and hoping.

The kind test suite kills the worker mid-run and asserts the run completes
with nothing leaked.

</details>

<details>
<summary><b>Standing vs. ephemeral agents</b></summary>

<br>

Poseidon distinguishes two kinds of agent, and the distinction is
structural rather than cosmetic.

A **standing agent** is one of the company's standing agents. It is declared in the
blueprint with a count, reconciled into a Deployment by the operator, and
has a stable identity that outlives any run. In the shipped example there is
exactly one: the PM.

An **ephemeral agent** exists only inside a run. It is bound when a
crew is fulfilled, gets a run-scoped identity, and is revoked at teardown.
It never appears in the standing agents and leaves no standing structure behind.

The rule the compiler enforces is that **execution is always ephemeral**. A
standing agent must be a *spawner* — a conductor with `may_spawn` authority.
A blueprint that tries to hire a standing `implementer` is a compile
error, because a standing agent holding write tools is exactly the thing
whose blast radius you cannot bound.

One consequence that surprises people: an ephemeral agent's tool scope is **not**
required to be a subset of its conductor's. The PM cannot open a pull
request, yet the crew it spawns can. Spawn authority is not a capability
ceiling — it is authority to *create a principal*, whose own scope comes
from the crew template. Keeping ⊆ would have forced the PM's scope to become
the union of everything it might ever spawn, which is the opposite of least
privilege.

Readers who know the capability literature will recognise the shape: the
conductor holds something like a *factory* — the right to mint a new
principal from a fixed, compiler-vetted template — rather than the right to
delegate its own authority. The blueprint bounds what the factory can
produce (`may_spawn` names crews, not tools), and compiled policy enforces
the bound at spawn time against a live concurrency count.

</details>
