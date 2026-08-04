"""`crew_requests` — recorded spawn authorization + intent, never
fulfillment (§4.4 rule 3, plan step 15).

One spawn model: whether the PM calls the `crew.request` tool (the gateway
records) or a workflow records under the PM's conductor authority (the
orchestrator, steps 18–19), the same row lands here and faced the same
policy. Rows are idempotent — the id derives from
{conductor, crew_template, run_id}, so a retried request refreshes rather
than duplicates — and expiring, so a request nothing fulfills orphans no
identity (§6.4). Fulfillment consumes them at step 16d.
"""

import hashlib
from typing import Any

DEFAULT_TTL_S = 3600

from registry.db import Registry


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
