"""`filed_tickets` — written by the gateway only.

The tickets the company filed, by repository and number, from the moment
the git host created them. The host lists a new issue a beat late; the
heartbeat's observation reads the ones filed within the host's lag window
by number and completes the listing with them, so the loop never misses a
ticket it filed itself. The console reads them all, for a person.
"""

from typing import Any

from registry.db import Registry


class FiledTickets:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def record(self, repo: str, number: int, run_id: str) -> None:
        """One row per ticket; recording it again is a no-op."""
        self._db.execute(
            """
            INSERT INTO registry.filed_tickets (repo, number, run_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (repo, number) DO NOTHING
            """,
            (repo, number, run_id),
        )

    def filed_since(self, repo: str, seconds: int) -> list[int]:
        """The numbers of the tickets filed on `repo` within the last
        `seconds`, ascending — the ones a listing may not carry yet."""
        rows = self._db.query(
            """
            SELECT number FROM registry.filed_tickets
             WHERE repo = %s AND filed_at > now() - make_interval(secs => %s)
             ORDER BY number
            """,
            (repo, seconds),
        )
        return [int(row["number"]) for row in rows]

    def filed(self, repo: str) -> list[dict[str, Any]]:
        """Every ticket filed on `repo`, ascending by number:
        `{number, run_id, filed_at}`."""
        return self._db.query(
            """
            SELECT number, run_id, filed_at FROM registry.filed_tickets
             WHERE repo = %s
             ORDER BY number
            """,
            (repo,),
        )
