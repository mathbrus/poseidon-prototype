"""`crew_requests` — recorded spawn authorization + intent, never
fulfillment.

One spawn model: whether the PM calls the `crew.request` tool (the gateway
records) or a workflow records under the PM's conductor authority (the
orchestrator), the same row lands here and faced the same
policy. Rows are idempotent — the id derives from
{conductor, crew_template, run_id}, so a retried request refreshes rather
than duplicates — and expiring, so a request nothing fulfills orphans no
identity. The orchestrator consumes them when it fulfills a crew.
"""

import hashlib
from typing import Any

from registry.db import Registry

DEFAULT_TTL_S = 3600


def request_id(conductor: str, crew_template: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{conductor}|{crew_template}|{run_id}".encode())
    return f"crq-{digest.hexdigest()[:16]}"


class CrewRequests:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def record(
        self,
        conductor: str,
        crew_template: str,
        run_id: str,
        reason: str,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> dict[str, Any]:
        """Record (or refresh) a request. Idempotent: the same
        {conductor, crew_template, run_id} lands on the same row; a
        still-`recorded` row gets its reason and expiry refreshed, while a
        fulfilled one is left untouched — a retry after fulfillment must
        not resurrect intent."""
        rid = request_id(conductor, crew_template, run_id)
        self._db.execute(
            """
            INSERT INTO registry.crew_requests
                (id, run_id, conductor, crew_template, reason, status, expires_at)
            VALUES (%s, %s, %s, %s, %s, 'recorded',
                    now() + make_interval(secs => %s))
            ON CONFLICT (id) DO UPDATE SET
                reason = EXCLUDED.reason,
                expires_at = EXCLUDED.expires_at
            WHERE registry.crew_requests.status = 'recorded'
            """,
            (rid, run_id, conductor, crew_template, reason, ttl_s),
        )
        row = self.get(rid)
        assert row is not None
        return row

    def consume(self, rid: str, run_id: str) -> dict[str, Any] | None:
        """Fulfillment's claim on a recorded request:
        recorded + unexpired -> fulfilled, atomically. Idempotent for the
        retrying activity: a request this run already fulfilled is handed
        back; anything else — expired, foreign, absent — is None, and
        fulfillment must not proceed (no orphan identities)."""
        rows = self._db.query(
            """
            UPDATE registry.crew_requests
               SET status = 'fulfilled', fulfilled_at = now()
             WHERE id = %s AND status = 'recorded'
               AND (expires_at IS NULL OR expires_at > now())
               AND run_id = %s
            RETURNING id
            """,
            (rid, run_id),
        )
        if rows:
            return self.get(rid)
        existing = self.get(rid)
        if (
            existing is not None
            and existing["status"] == "fulfilled"
            and existing["run_id"] == run_id
        ):
            return existing  # the retry after a crash mid-fulfill
        return None

    def get(self, rid: str) -> dict[str, Any] | None:
        rows = self._db.query(
            "SELECT id, run_id, conductor, crew_template, reason, status,"
            " created_at, expires_at, fulfilled_at"
            " FROM registry.crew_requests WHERE id = %s",
            (rid,),
        )
        if not rows:
            return None
        row = dict(rows[0])
        for key in ("created_at", "expires_at", "fulfilled_at"):
            if row.get(key) is not None:
                row[key] = row[key].isoformat()
        return row
