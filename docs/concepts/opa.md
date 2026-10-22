# OPA and Rego

[Open Policy Agent](https://www.openpolicyagent.org/) (OPA) is a
general-purpose policy engine. You give it rules written in a language
called **Rego**, then ask it questions: "given this input, what is the
decision?"

## Why Poseidon uses it

A decision is a pure function of the policy and the input. OPA reads no
live data of its own, so the same input always gets the same answer, even
after a restart.

Every gateway and the console ask OPA before they act. The policy they ask
is compiled from your blueprint. Nobody writes Rego by hand, so a
permission you didn't declare can't exist.

## What a rule looks like

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

Read it as: *an ephemeral agent of hello-saas, acting as the implementer
of a feature-delivery crew, may call `sandbox.exec`, provided it is bound
to the run it names.*

`bound_to_run` matters. Without it, any implementer could reach any run's
sandbox. With it, an agent reaches only the sandbox of its own run.

Everything is denied unless a rule allows it, and any deny beats every
allow. [Policy and audit](../reference/policy.md) lists every rule and what
a decision reads.
