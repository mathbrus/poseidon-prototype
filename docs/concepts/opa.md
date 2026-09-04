# OPA and Rego — policy as a pure function

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
any allow. [Policy and audit](../reference/policy.md) lists the whole rule
vocabulary and what a decision reads.

That final clause, `bound_to_run`, deserves a second look. Sandbox tools
are *run-scoped*: the rule demands not just the right role but a verified
binding between this caller and the specific run whose sandbox the call
addresses. Without it, a role allow-list would quietly permit cross-run
access — any implementer could read any run's workspace. Which tools carry
the requirement is a property of the tool, not of its name prefix
(`packages/schema/src/schema/tools.py` maintains the list); `git.open_pr`,
for instance, does not, because a pull request targets the repository
rather than a run's sandbox — its guards are the branch-protection and
changed-path rules you will meet in [the blueprint](../reference/blueprint.md).

**Nobody writes these by hand.** They are emitted by the compiler from the
blueprint, which is why a scope you did not declare cannot exist.
