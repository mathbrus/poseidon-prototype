"""`policy_bundles` — written by the operator.

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


def _tarball(files: dict[str, str], manifest: dict[str, object]) -> bytes:
    """The bundle tarball, with fixed tar metadata: identical content must
    produce an identical tarball, or the revision would churn on every
    reconcile."""
    body = json.dumps(manifest) + "\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, text in [*sorted(files.items()), (".manifest", body)]:
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0  # deterministic: content decides the revision
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def pack(files: dict[str, str]) -> tuple[bytes, str]:
    """Build an OPA bundle tarball and its revision.

    The revision is a digest of the content, so re-applying an unchanged
    blueprint is a no-op for OPA and a changed one is unmissable.
    """
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update(files[name].encode())
    revision = digest.hexdigest()[:16]
    return _tarball(files, {"revision": revision, "roots": _roots(files)}), revision


def tombstone(roots: list[str]) -> tuple[bytes, str]:
    """An empty bundle that still declares its roots: how a bundle revokes.

    Activation erases everything under the roots that the bundle does not
    supply, and a tombstone supplies nothing, so OPA's next poll leaves no
    rule under them — the decision path answers no document, the same
    fail-closed default as a company never applied. The roots are the
    whole content, so they are the revision. An empty roots list would
    claim the entire tree and erase every other bundle; it is refused.
    """
    if not roots:
        raise ValueError("a tombstone must declare the roots it revokes")
    digest = hashlib.sha256(b"tombstone")
    for root in sorted(roots):
        digest.update(root.encode())
    revision = digest.hexdigest()[:16]
    return _tarball({}, {"revision": revision, "roots": sorted(roots)}), revision


def _manifest_roots(tarball: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        return json.loads(tar.extractfile(".manifest").read())["roots"]


@dataclass(frozen=True)
class StoredBundle:
    company: str
    revision: str
    tarball: bytes
    updated_at: datetime


class PolicyBundles:
    """The bundles OPA polls, one row per slot (`platform`, `company`).
    The column is still called `company`: it holds the slot name."""

    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def put(self, slot: str, files: dict[str, str]) -> str:
        tarball, revision = pack(files)
        self._store(slot, revision, tarball)
        return revision

    def _store(self, slot: str, revision: str, tarball: bytes) -> None:
        self._db.execute(
            """
            INSERT INTO registry.policy_bundles (company, revision, tarball, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (company) DO UPDATE SET
                revision = EXCLUDED.revision,
                tarball = EXCLUDED.tarball,
                updated_at = now()
            """,
            (slot, revision, tarball),
        )

    def get(self, slot: str) -> StoredBundle | None:
        rows = self._db.query(
            "SELECT company, revision, tarball, updated_at"
            " FROM registry.policy_bundles WHERE company = %s",
            (slot,),
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

    def revoke(self, slot: str) -> str | None:
        """Company teardown revokes policy: the stored bundle is replaced by
        a tombstone over the same roots, still served, so a running OPA
        drops the company's rules on its own next poll — no restart — and
        a restarted one activates the same nothing. Returns the tombstone's
        revision, or None when nothing was stored to revoke."""
        stored = self.get(slot)
        if stored is None:
            return None
        tarball, revision = tombstone(_manifest_roots(stored.tarball))
        self._store(slot, revision, tarball)
        return revision
