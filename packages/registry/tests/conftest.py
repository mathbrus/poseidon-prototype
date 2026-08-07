"""Registry tests run against a real Postgres — the SQL is the thing under
test, so a fake would test nothing.

Point REGISTRY_DSN at any database (CI runs a `postgres` service container;
locally, `kubectl -n poseidon-platform port-forward svc/postgres 5432:5432`
against the kind cluster works). Without one the suite skips rather than
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
    """Each test owns the tables it touches; ordering follows the foreign
    key from run_participants to runs."""
    yield
    registry.execute("DELETE FROM registry.audit_events")
    registry.execute("DELETE FROM registry.sandbox_registrations")
    registry.execute("DELETE FROM registry.run_participants")
    registry.execute("DELETE FROM registry.runs")
