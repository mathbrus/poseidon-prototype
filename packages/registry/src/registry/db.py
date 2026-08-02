"""Connection handling and migration.

A pooled psycopg connection per process, opened lazily so a service can
start before Postgres is ready (on a fresh cluster the poseidon release
installs before the database accepts connections). Calls block until the
pool can hand out a connection or the timeout expires — the alternative,
degrading silently when the registry is unreachable, would make run records
and audit events optional, which is the opposite of what they are for.
"""

import os
from importlib import resources
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from common.telemetry import get_logger

DEFAULT_DSN = "postgresql://poseidon@postgres:5432/poseidon"
# Long enough to cover a Postgres restart or a cold pool, short enough that
# a genuinely absent database surfaces as an error rather than a hang.
CONNECT_TIMEOUT_S = 30
# One lock id for the whole schema: migration is fast and rare, and
# concurrent CREATE TABLE IF NOT EXISTS can still collide in Postgres.
_MIGRATION_LOCK = 8_675_309


def dsn() -> str:
    return os.environ.get("REGISTRY_DSN", DEFAULT_DSN)


class Registry:
    """Owns the pool. Services construct one and keep it for their lifetime."""

    def __init__(self, conninfo: str | None = None) -> None:
        self._pool = ConnectionPool(
            conninfo or dsn(),
            min_size=1,
            max_size=8,
            timeout=CONNECT_TIMEOUT_S,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        self._pool.open()
        self._log = get_logger("registry")

    def connection(self):
        return self._pool.connection()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.connection() as conn:
            conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def migrate(self) -> None:
        """Apply the schema. Idempotent and safe to run from every service
        at startup, in any order, concurrently."""
        ddl = resources.files("registry").joinpath("schema.sql").read_text()
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                conn.execute(ddl)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
        self._log.info("registry schema ready")

    def close(self) -> None:
        self._pool.close()


def open_registry(conninfo: str | None = None, migrate: bool = True) -> Registry:
    registry = Registry(conninfo)
    if migrate:
        registry.migrate()
    return registry


__all__ = ["Connection", "Registry", "dsn", "open_registry"]
