"""`companies` — written by the operator: the registry's record of what has
been applied, upserted on reconcile and cleared on teardown.
"""

from typing import Any

from psycopg.types.json import Jsonb

from registry.db import Registry


class Companies:
    """The companies table — the registry's record of what has been applied,
    written by the operator on reconcile and cleared on teardown."""

    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def upsert(self, name: str, spec: dict[str, Any]) -> None:
        self._db.execute(
            """
            INSERT INTO registry.companies (name, blueprint, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                blueprint = EXCLUDED.blueprint,
                updated_at = now()
            """,
            (name, Jsonb(spec)),
        )

    def get(self, name: str) -> dict[str, Any] | None:
        """The applied spec (the Company CR spec minus the bundle) — what the
        orchestrator's provision reads the infra IR from."""
        rows = self._db.query(
            "SELECT blueprint FROM registry.companies WHERE name = %s", (name,)
        )
        return rows[0]["blueprint"] if rows else None

    def applied(self) -> str | None:
        """The name of the one applied company, or None before the first
        apply. A platform hosts one company: a second row is a fault, not
        a choice, so it is raised rather than picked from."""
        rows = self._db.query("SELECT name FROM registry.companies ORDER BY name")
        if len(rows) > 1:
            names = [row["name"] for row in rows]
            raise RuntimeError(f"more than one company is applied: {names}")
        return rows[0]["name"] if rows else None

    def by_namespace(self, namespace: str) -> dict[str, Any] | None:
        """Resolve a company namespace back to its applied spec — the egress
        proxy's path from a run's sandbox namespace to the blueprint's
        allowlist projection."""
        rows = self._db.query(
            "SELECT blueprint FROM registry.companies WHERE blueprint->>'namespace' = %s",
            (namespace,),
        )
        return rows[0]["blueprint"] if rows else None

    def delete(self, name: str) -> None:
        self._db.execute("DELETE FROM registry.companies WHERE name = %s", (name,))
