"""`agent_instances` — written by the operator: the standing agents, and
the gateways' read side for workload identity.
"""

from registry.db import Registry


class AgentInstances:
    """Standing agents status, written by the operator."""

    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def upsert(
        self,
        agent_id: str,
        company: str,
        standing_name: str,
        role: str,
        namespace: str | None = None,
        status: str = "ready",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO registry.agent_instances
                (agent_id, company, standing_name, role, namespace,
                 status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (agent_id) DO UPDATE SET
                company = EXCLUDED.company,
                standing_name = EXCLUDED.standing_name,
                role = EXCLUDED.role,
                namespace = EXCLUDED.namespace,
                status = EXCLUDED.status,
                updated_at = now()
            """,
            (
                agent_id,
                company,
                standing_name,
                role,
                namespace,
                status,
            ),
        )

    def get(self, agent_id: str) -> dict | None:
        """One standing instance — the gateway's read for workload
        identity: a projected token's ServiceAccount name resolves
        here, and its namespace must match the recorded one."""
        rows = self._db.query(
            "SELECT agent_id, company, standing_name, role,"
            " namespace, status"
            " FROM registry.agent_instances WHERE agent_id = %s",
            (agent_id,),
        )
        return rows[0] if rows else None

    def delete(self, agent_id: str) -> None:
        self._db.execute(
            "DELETE FROM registry.agent_instances WHERE agent_id = %s", (agent_id,)
        )

    def delete_for_company(self, company: str) -> None:
        """The company-level sweep at teardown: correct even if the operator
        was down for individual instance-delete events."""
        self._db.execute(
            "DELETE FROM registry.agent_instances WHERE company = %s", (company,)
        )

    def for_company(self, company: str) -> list[dict]:
        return self._db.query(
            "SELECT agent_id, company, standing_name, role,"
            " namespace, status"
            " FROM registry.agent_instances WHERE company = %s ORDER BY standing_name",
            (company,),
        )
