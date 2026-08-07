"""The Poseidon operator.

Reconciles `Company` and `AgentInstance` custom resources into a live
company: namespace-per-company, the deny-all NetworkPolicy envelope,
standing agent Deployments, and compiled policy bundles persisted for the
bundle server to publish. Writes instance status to the registry.

Hard boundaries: it never creates ephemeral agents —
those exist only inside a run and belong to the orchestrator's fulfill
activities — and it never runs a process. The importable name is
`poseidon_operator` because `operator` is a Python standard-library module.
"""
