"""`run_participants` — ephemeral agents, one row per bound identity.

Both halves of the ephemeral-identity contract meet here. The verify half:
the gateway resolves an ephemeral agent's projected ServiceAccount token to a
live row (by `service_account`) and builds the caller's identity entirely
from it — company, catalog role, namespace, crew. The fulfill half: the
orchestrator's bind activity creates the row, and revocation is a row
termination — checked on every call, terminated on every terminal path
(the workflow's compensation sweeps the run).
"""

from typing import Any

from registry.db import Registry

_ROW_FIELDS = (
    "agent_id, run_id, parent_agent_id, crew_template, role,"
    " company, namespace, service_account, status"
)


class ParticipantRevoked(Exception):
    """Raised when a bind would revive an already-revoked binding row —
    i.e. a retry that lost the race with its run's teardown.
    Revocation is final: the caller must abandon the identity, not rebuild
    on it."""


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
        company: str = "",
        namespace: str = "",
        service_account: str | None = None,
    ) -> None:
        """Create the binding row, or re-assert one that is still active.
        Upsert on purpose: the bind activity derives a deterministic
        agent_id from {crew, role, run}, so a retry after a worker death
        lands on the same row instead of leaking a second identity.

        **Revocation is final**: a terminated row is never
        revived. The only way to reach a terminated row is to bind after
        the run's teardown already swept it — a retry racing compensation,
        which must not resurrect an identity the platform has revoked.
        `ParticipantRevoked` tells that caller to stop rather than build on
        a dead binding.
        """
        updated = self._db.query(
            """
            INSERT INTO registry.run_participants
                (agent_id, run_id, parent_agent_id, crew_template, role,
                 company, namespace, service_account, status, terminated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', NULL)
            ON CONFLICT (agent_id) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                parent_agent_id = EXCLUDED.parent_agent_id,
                crew_template = EXCLUDED.crew_template,
                role = EXCLUDED.role,
                company = EXCLUDED.company,
                namespace = EXCLUDED.namespace,
                service_account = EXCLUDED.service_account
            WHERE run_participants.status = 'active'
            RETURNING agent_id
            """,
            (
                agent_id,
                run_id,
                parent_agent_id,
                crew_template,
                role,
                company,
                namespace,
                service_account,
            ),
        )
        # No row back means the conflict target existed and was terminated:
        # the WHERE on the DO UPDATE suppressed it.
        if not updated:
            raise ParticipantRevoked(
                f"participant {agent_id} was revoked; it is not revivable"
            )

    def terminate(self, agent_id: str) -> None:
        """Revocation: from this write on, the gateway finds no live
        row and the identity's tokens are dead — however long the
        projected token itself would remain valid."""
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

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Every identity ever bound to a run, terminated rows included —
        the run's identity lineage. The reviewer-independence check
        compares lineages, and an implement run's participants are always
        revoked by the time its PR is reviewed, so an active-only read
        would vacuously pass the check it exists to make."""
        return self._db.query(
            "SELECT agent_id, run_id, crew_template, role,"
            " status FROM registry.run_participants WHERE run_id = %s"
            " ORDER BY agent_id",
            (run_id,),
        )

    def active_spawn_count(self, parent_agent_id: str) -> int:
        """How many crews the conductor is running right now — distinct
        {run, crew} pairs with live ephemeral agents. What the gateway injects as
        `active_spawns` for the compiled concurrency cap."""
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
            f"SELECT {_ROW_FIELDS}"
            " FROM registry.run_participants"
            " WHERE agent_id = %s AND status = 'active' AND terminated_at IS NULL",
            (agent_id,),
        )
        return rows[0] if rows else None

    def by_service_account(self, service_account: str) -> dict[str, Any] | None:
        """The live row backing a per-run ServiceAccount — what the gateway
        resolves a TokenReview-verified crew token against. None once the
        row is terminated: revocation is a registry write, not an expiry."""
        rows = self._db.query(
            f"SELECT {_ROW_FIELDS}"
            " FROM registry.run_participants"
            " WHERE service_account = %s"
            "   AND status = 'active' AND terminated_at IS NULL",
            (service_account,),
        )
        return rows[0] if rows else None
