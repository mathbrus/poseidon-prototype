"""`companies`, `policy_bundles` and `agent_instances` — operator-written.

The bundle delivery contract in one place: the operator persists a compiled
bundle, the bundle server hands it to OPA over the standard bundle API, and
OPA polls. Nothing is pushed at runtime, so a gateway or OPA restart
re-fetches the same revision instead of running on whatever it last held.
"""

import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from registry.db import Registry

_PACKAGE = re.compile(r"^package\s+([A-Za-z0-9_.]+)", re.MULTILINE)


def _roots(files: dict[str, str]) -> list[str]:
    """Bundle roots, derived from the files' own `package` declarations.

    Activation erases everything under the roots that the bundle does not
    supply, so the roots must cover exactly what the bundle contains — a
    root any wider would delete policy another bundle owns (company
    packages are per-company and the platform package is its own bundle
    precisely so the roots stay disjoint). Deriving keeps this file and the
    compiler's package names from ever drifting apart.
    """
    roots = set()
    for name, body in files.items():
        match = _PACKAGE.search(body)
        if match is None:
            raise ValueError(f"bundle file {name!r} has no package declaration")
        roots.add(match.group(1).replace(".", "/"))
    return sorted(roots)


def pack(files: dict[str, str]) -> tuple[bytes, str]:
    """Build an OPA bundle tarball and its revision.

    The revision is a digest of the content, so re-applying an unchanged
    blueprint is a no-op for OPA and a changed one is unmissable. Tar
    metadata is fixed for the same reason: identical policy must produce an
    identical tarball, or the revision would churn on every reconcile.
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update(files[name].encode())
    revision = digest.hexdigest()[:16]

    manifest = json.dumps({"revision": revision, "roots": _roots(files)}) + "\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, body in [*sorted(files.items()), (".manifest", manifest)]:
            payload = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0  # deterministic: content decides the revision
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue(), revision


@dataclass(frozen=True)
class StoredBundle:
    company: str
    revision: str
    tarball: bytes
    updated_at: datetime


class PolicyBundles:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def put(self, company: str, files: dict[str, str]) -> str:
        tarball, revision = pack(files)
        self._db.execute(
            """
            INSERT INTO registry.policy_bundles (company, revision, tarball, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (company) DO UPDATE SET
                revision = EXCLUDED.revision,
                tarball = EXCLUDED.tarball,
                updated_at = now()
            """,
            (company, revision, tarball),
        )
        return revision

    def get(self, company: str) -> StoredBundle | None:
        rows = self._db.query(
            "SELECT company, revision, tarball, updated_at"
            " FROM registry.policy_bundles WHERE company = %s",
            (company,),
        )
        if not rows:
            return None
        row = rows[0]
        return StoredBundle(
            company=row["company"],
            revision=row["revision"],
            tarball=bytes(row["tarball"]),
            updated_at=row["updated_at"] or datetime.now(UTC),
        )

    def delete(self, company: str) -> None:
        """Company teardown revokes policy: the bundle stops being served,
        so a caller that still holds an identity finds no rule to match."""
        self._db.execute(
            "DELETE FROM registry.policy_bundles WHERE company = %s", (company,)
        )


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


class AgentInstances:
    """Standing roster status, written by the operator."""

    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def upsert(
        self,
        agent_id: str,
        company: str,
        hire: str,
        role_template: str,
        model_tier: str,
        namespace: str | None = None,
        status: str = "ready",
    ) -> None:
        self._db.execute(
            """
            INSERT INTO registry.agent_instances
                (agent_id, company, hire, role_template, model_tier, namespace,
                 status, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (agent_id) DO UPDATE SET
                company = EXCLUDED.company,
                hire = EXCLUDED.hire,
                role_template = EXCLUDED.role_template,
                model_tier = EXCLUDED.model_tier,
                namespace = EXCLUDED.namespace,
                status = EXCLUDED.status,
                updated_at = now()
            """,
            (agent_id, company, hire, role_template, model_tier, namespace, status),
        )

    def get(self, agent_id: str) -> dict | None:
        """One standing instance — the gateway's read for workload
        identity: a projected token's ServiceAccount name resolves
        here, and its namespace must match the recorded one."""
        rows = self._db.query(
            "SELECT agent_id, company, hire, role_template, model_tier,"
            " namespace, status"
            " FROM registry.agent_instances WHERE agent_id = %s",
            (agent_id,),
        )
        return rows[0] if rows else None

    def delete(self, agent_id: str) -> None:
        self._db.execute(
            "DELETE FROM registry.agent_instances WHERE agent_id = %s", (agent_id,)
        )

    def delete_for_company(self, company: str) -> None:
        """The company-level sweep at teardown: correct even if the operator
        was down for individual instance-delete events."""
        self._db.execute(
            "DELETE FROM registry.agent_instances WHERE company = %s", (company,)
        )

    def for_company(self, company: str) -> list[dict]:
        return self._db.query(
            "SELECT agent_id, company, hire, role_template, model_tier,"
            " namespace, status"
            " FROM registry.agent_instances WHERE company = %s ORDER BY hire",
            (company,),
        )
