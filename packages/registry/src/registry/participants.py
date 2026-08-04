"""`run_participants` — ephemeral crew members, one row per minted identity.

Write ownership belongs to the orchestrator's fulfill activities (plan step
16a/16b: created at fulfill, terminated on every terminal path). What lands
at step 11 is the *verify* half of the §2b identity contract: the gateway
accepts a platform-signed JWT only while a live row backs it, so revocation
is a row termination and takes effect before the token's TTL runs out.
"""

from typing import Any

from registry.db import Registry


class RunParticipants:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def active(self, agent_id: str) -> dict[str, Any] | None:
        """The live row for an ephemeral identity — None once terminated,
        which is exactly when its token must stop working."""
        rows = self._db.query(
            "SELECT agent_id, run_id, parent_agent_id, crew_template, role, status"
            " FROM registry.run_participants"
            " WHERE agent_id = %s AND status = 'active' AND terminated_at IS NULL",
            (agent_id,),
        )
        return rows[0] if rows else None
