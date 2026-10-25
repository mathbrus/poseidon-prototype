"""`companies` — the registry's record of what has been applied: the
compiled spec, written by the reconciler on reconcile and erased with
every other row of the company by the teardown sweep (`registry.sweep`);
and the pause, written by the console under a human's act and
read by the heartbeat on every tick. The blueprint a human wrote is not
here: its text stays on the Company CR, where the console reads it back.
"""

from typing import Any

from psycopg.types.json import Jsonb

from registry.db import Registry


class Companies:
    """The companies table — the compiled spec and the pause."""

    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def upsert(self, name: str, spec: dict[str, Any]) -> None:
        """The compiled spec, replaced on every reconcile. The pause columns
        are not touched: a paused company whose blueprint is applied again
        stays paused until a human resumes it."""
        self._db.execute(
            """
            INSERT INTO registry.companies (name, compiled_spec, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                compiled_spec = EXCLUDED.compiled_spec,
                updated_at = now()
            """,
            (name, Jsonb(spec)),
        )

    def pause(self, name: str, by: str) -> dict[str, Any]:
        """Pause the heartbeat under `by`'s name; the time is the
        registry's. Returns the fact as `paused` reads it."""
        self._db.execute(
            "UPDATE registry.companies SET paused_by = %s, paused_at = now()"
            " WHERE name = %s",
            (by, name),
        )
        paused = self.paused(name)
        assert paused is not None, f"no company {name!r} to pause"
        return paused

    def resume(self, name: str) -> None:
        self._db.execute(
            "UPDATE registry.companies SET paused_by = NULL, paused_at = NULL"
            " WHERE name = %s",
            (name,),
        )

    def paused(self, name: str) -> dict[str, Any] | None:
        """`{by, at}` while the company is paused (`at` ISO-8601), else None —
        also None for a company that is not applied."""
        rows = self._db.query(
            "SELECT paused_by, paused_at FROM registry.companies"
            " WHERE name = %s AND paused_by IS NOT NULL",
            (name,),
        )
        if not rows:
            return None
        return {"by": rows[0]["paused_by"], "at": rows[0]["paused_at"].isoformat()}

    def get(self, name: str) -> dict[str, Any] | None:
        """The compiled spec (the Company CR's spec less the bundle and the
        blueprint's text) — what the platform enforces and reads: the
        infra IR provision reads, the caps, the bounds."""
        rows = self._db.query(
            "SELECT compiled_spec FROM registry.companies WHERE name = %s", (name,)
        )
        return rows[0]["compiled_spec"] if rows else None

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
        """Resolve a company namespace back to its compiled spec — the egress
        proxy's path from a run's sandbox namespace to the blueprint's
        allowlist projection."""
        rows = self._db.query(
            "SELECT compiled_spec FROM registry.companies"
            " WHERE compiled_spec->>'namespace' = %s",
            (namespace,),
        )
        return rows[0]["compiled_spec"] if rows else None
