"""Ephemeral identity minting — the fulfill-side half of the
ephemeral-identity contract.

The orchestrator is the only minter: it signs short-TTL platform JWTs
carrying the identity claims, and the gateways accept one only while a live
`run_participants` row backs it.
Revocation is therefore a registry row termination — effective before the
TTL runs out.

Agent ids are deterministic from {crew, member, run}: a mint retried after
a worker death lands on the same participants row instead of leaking a
second identity.
"""

from dataclasses import dataclass

from common import platformjwt

# Short by design: the row termination is the real revocation; the TTL just
# bounds how long a leaked token idles if the registry were unreachable.
DEFAULT_TTL_S = 900
AUDIENCE = "poseidon-gateway"


def ephemeral_agent_id(crew_template: str, member: str, run_id: str) -> str:
    """One identity per crew member per run, deterministically named — the
    `agent_id` of an ephemeral caller, and the participants row's key."""
    return f"{crew_template}-{member}-{run_id}"


@dataclass(frozen=True)
class MintedIdentity:
    agent_id: str
    token: str
    ttl_s: int


def mint_ephemeral(
    key: str,
    *,
    agent_id: str,
    company: str,
    role_template: str,
    run_id: str,
    parent_agent_id: str,
    ttl_s: int = DEFAULT_TTL_S,
) -> MintedIdentity:
    """Sign the identity claims. `role` carries the catalog template (what
    path-ownership rules name); the crew coordinates the compiled rules key
    on come from the live participants row at verification time."""
    token = platformjwt.mint(
        key,
        {
            "sub": agent_id,
            "company": company,
            "role": role_template,
            "lifecycle": "ephemeral",
            "run_id": run_id,
            "parent_agent_id": parent_agent_id,
        },
        AUDIENCE,
        ttl_s=ttl_s,
    )
    return MintedIdentity(agent_id=agent_id, token=token, ttl_s=ttl_s)
