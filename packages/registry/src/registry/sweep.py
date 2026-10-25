"""The teardown sweep — called by the reconciler only, once a deleted
company's namespace is gone.

Every table has one writer while its company lives; the sweep is the one
deleter of every table. It erases every row the platform recorded about
the company, in one transaction: a company applied again under the same
name starts with no history to inherit. What it leaves is the platform's
own: the policy bundles (the company slot holds the tombstone, a
revocation rather than a record) and audit rows that name neither the
company nor one of its runs.

A row belongs to the company by its company column or, where a writer
knows only the run, by run id: egress attempts, the platform identity's
calls under the heartbeat's run id, the bootstrap tickets it filed. The
runs are the company's runs plus the heartbeat's own run id.
"""

from common.workflows import heartbeat_run_id
from registry.db import Registry


class CompanySweep:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def sweep(self, company: str) -> None:
        """Delete every row of `company`. Repeating it finds nothing."""
        # The heartbeat's run id, which is also its workflow id: the run
        # id every heartbeat row carries, and the workflow its ticks name.
        heartbeat = heartbeat_run_id(company)
        with self._db.transaction() as conn:
            # Locked first: a writer that may only write while the company
            # is applied (`AuditStore.append_while_applied`) either commits
            # before this sweep reads anything, or finds the row gone.
            applied = conn.execute(
                "SELECT compiled_spec->'repo'->>'url' AS repo"
                " FROM registry.companies WHERE name = %s FOR UPDATE",
                (company,),
            ).fetchone()
            repo = applied["repo"] if applied else None
            run_ids = [heartbeat] + [
                row["run_id"]
                for row in conn.execute(
                    "SELECT run_id FROM registry.runs WHERE company = %s", (company,)
                )
            ]
            conn.execute(
                "DELETE FROM registry.audit_events"
                " WHERE company = %s OR run_id = ANY(%s) OR workflow = %s",
                (company, run_ids, heartbeat),
            )
            conn.execute(
                "DELETE FROM registry.budget_ledger"
                " WHERE company = %s OR run_id = ANY(%s)",
                (company, run_ids),
            )
            conn.execute(
                "DELETE FROM registry.sandbox_registrations WHERE run_id = ANY(%s)",
                (run_ids,),
            )
            conn.execute(
                "DELETE FROM registry.run_participants"
                " WHERE company = %s OR run_id = ANY(%s)",
                (company, run_ids),
            )
            conn.execute(
                "DELETE FROM registry.filed_tickets WHERE repo = %s OR run_id = ANY(%s)",
                (repo, run_ids),
            )
            conn.execute("DELETE FROM registry.runs WHERE company = %s", (company,))
            conn.execute(
                "DELETE FROM registry.agent_instances WHERE company = %s", (company,)
            )
            conn.execute("DELETE FROM registry.companies WHERE name = %s", (company,))
