"""Ephemeral identity naming — the fulfill-side half of the
ephemeral-identity contract.

There is nothing to mint: a crew member's credential is the audience-scoped
projected ServiceAccount token the kubelet mounts into its pod, exactly as
for a standing agent. What the orchestrator contributes is the *binding* —
the `run_participants` row that maps the member's per-run ServiceAccount to
its run, crew coordinates and catalog template. The gateways resolve a
TokenReview-verified crew token against that row on every call, so
revocation is a row termination, effective immediately.

Agent ids are deterministic from {crew, member, run}: a bind retried after
a worker death lands on the same participants row instead of leaking a
second identity.
"""


def ephemeral_agent_id(crew_template: str, member: str, run_id: str) -> str:
    """One identity per crew member per run, deterministically named — the
    `agent_id` of an ephemeral caller, and the participants row's key."""
    return f"{crew_template}-{member}-{run_id}"
