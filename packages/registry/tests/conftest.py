"""Registry tests run against a real Postgres — the SQL is the thing under
test, so a fake would test nothing.

Point REGISTRY_DSN at a throwaway database (CI runs a `postgres` service
container; locally, `docker run --rm -p 5433:5432 -e POSTGRES_PASSWORD=x
postgres:16.6`). Never at the kind cluster's registry: the `clean` fixture
deletes every row of every table. Without one the suite skips rather than
failing, so a laptop without a database still gets a green unit run.
"""

import os

import pytest

from registry.db import Registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    dsn = os.environ.get("REGISTRY_DSN")
    if not dsn:
        pytest.skip("no REGISTRY_DSN — set it to run the registry SQL tests")
    try:
        db = Registry(dsn)
        db.migrate()
    except Exception as exc:  # unreachable database
        pytest.skip(f"registry database unreachable: {exc}")
    return db


@pytest.fixture(autouse=True)
def clean(registry: Registry):
    """Every table starts empty for the next test — all nine, so no test
    needs its own cleanup; run_participants goes before runs (the foreign
    key)."""
    yield
    for table in (
        "audit_events",
        "sandbox_registrations",
        "filed_tickets",
        "run_participants",
        "runs",
        "budget_ledger",
        "agent_instances",
        "policy_bundles",
        "companies",
    ):
        registry.execute(f"DELETE FROM registry.{table}")
