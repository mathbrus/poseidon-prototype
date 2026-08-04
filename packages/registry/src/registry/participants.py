"""`run_participants` — ephemeral crew members, one row per minted identity.

Both halves of the frozen §2b identity contract meet here. The verify half
(step 11): the gateway accepts a platform-signed JWT only while a live row
backs it. The fulfill half (step 16a): the orchestrator's mint activity
creates the row, and revocation is a row termination — effective before the
token's TTL runs out, terminated on every terminal path (16b/16d extend the
sweep to the workflow's compensation).

The `role` column stores the crew member name — what §5.1 calls `crew_role`
and the compiled crew rules key on; the §5.1 `role` (the catalog template)
rides the minted JWT's claims.
"""

from typing import Any

from registry.db import Registry


class RunParticipants:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def add(
        self,
        agent_id: str,
        run_id: str,
        parent_agent_id: str,
        crew_template: str,
        role: str,
    ) -> None:
        """Create (or revive) the binding row. Upsert on purpose: the mint
        activity derives a deterministic agent_id from
        {crew, member, run}, so a retry after a worker death lands on the
        same row instead of leaking a second identity."""
        self._db.execute(
            """
            INSERT INTO registry.run_participants
                (agent_id, run_id, parent_agent_id, crew_template, role,
                 status, terminated_at)
            VALUES (%s, %s, %s, %s, %s, 'active', NULL)
            ON CONFLICT (agent_id) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                parent_agent_id = EXCLUDED.parent_agent_id,
                crew_template = EXCLUDED.crew_template,
                role = EXCLUDED.role,
                status = 'active',
                terminated_at = NULL
            """,
            (agent_id, run_id, parent_agent_id, crew_template, role),
        )

    def terminate(self, agent_id: str) -> None:
        """Revocation (§2b): from this write on, the gateway finds no live
        row and the identity's tokens are dead — before TTL expiry."""
        self._db.execute(
            "UPDATE registry.run_participants"
            " SET status = 'terminated', terminated_at = now()"
            " WHERE agent_id = %s AND status = 'active'",
            (agent_id,),
        )

    def terminate_for_run(self, run_id: str) -> list[str]:
        """The terminal-path sweep: every live participant of the run is
        revoked at once — correct even if individual revokes were missed.
        Returns the swept agent ids (each becomes an audit event)."""
        rows = self._db.query(
            """
            UPDATE registry.run_participants
               SET status = 'terminated', terminated_at = now()
             WHERE run_id = %s AND status = 'active'
            RETURNING agent_id
            """,
            (run_id,),
        )
        return [row["agent_id"] for row in rows]

    def active_spawn_count(self, parent_agent_id: str) -> int:
        """How many crews the conductor is running right now — distinct
        {run, crew} pairs with live members. What the gateway injects as
        `active_spawns` for the compiled concurrency cap (plan step 16d)."""
        rows = self._db.query(
            "SELECT COUNT(DISTINCT (run_id, crew_template)) AS spawns"
            " FROM registry.run_participants"
            " WHERE parent_agent_id = %s AND status = 'active'"
            "   AND terminated_at IS NULL",
            (parent_agent_id,),
        )
        return int(rows[0]["spawns"])

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
