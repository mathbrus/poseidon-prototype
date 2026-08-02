"""`sandbox_registrations` — written by the gateway only.

Replaces the gateway's in-memory `_sandboxes` dict (resolution row 16). The
dict was fine while runs were a host-side script; once runs are durable, a
gateway restart mid-run silently orphaned every sandbox proxy call, and the
orchestrator had no way to know.
"""

from dataclasses import dataclass

from registry.db import Registry


@dataclass
class SandboxRegistration:
    run_id: str
    url: str
    namespace: str | None = None
    job_name: str | None = None


class SandboxRegistrations:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def register(
        self,
        run_id: str,
        url: str,
        namespace: str | None = None,
        job_name: str | None = None,
    ) -> None:
        """One sandbox per run, so a re-registration is an update: the
        provision activity's retry after a crash re-registers a new address
        for the same run."""
        self._db.execute(
            """
            INSERT INTO registry.sandbox_registrations
                (run_id, url, namespace, job_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                url = EXCLUDED.url,
                namespace = EXCLUDED.namespace,
                job_name = EXCLUDED.job_name,
                terminated_at = NULL
            """,
            (run_id, url, namespace, job_name),
        )

    def url_for(self, run_id: str) -> str | None:
        """The address a `sandbox.*` call proxies to. Terminated rows return
        nothing: a call against a torn-down sandbox must fail, not reach
        whatever now answers at that address."""
        rows = self._db.query(
            """
            SELECT url FROM registry.sandbox_registrations
             WHERE run_id = %s AND terminated_at IS NULL
            """,
            (run_id,),
        )
        return rows[0]["url"] if rows else None

    def terminate(self, run_id: str) -> None:
        self._db.execute(
            """
            UPDATE registry.sandbox_registrations
               SET terminated_at = now()
             WHERE run_id = %s AND terminated_at IS NULL
            """,
            (run_id,),
        )
