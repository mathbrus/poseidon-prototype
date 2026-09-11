"""The bundle server: standard OPA bundle semantics over stored bundles."""

import io
import tarfile
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from poseidon_operator.bundleserver import create_app
from registry import StoredBundle, pack, tombstone


def stored(company: str, files: dict[str, str]) -> StoredBundle:
    tarball, revision = pack(files)
    return StoredBundle(company, revision, tarball, datetime.now(UTC))


class FakeBundles:
    """The bundle server's read side over a dict of stored bundles."""

    def __init__(self, *bundles: StoredBundle) -> None:
        self.stored = {b.company: b for b in bundles}

    def get(self, company: str) -> StoredBundle | None:
        return self.stored.get(company)


def test_bundle_is_served_as_a_tarball_with_its_revision():
    bundle = stored("hello-saas", {"authz.rego": "package poseidon.authz\n"})
    revision = bundle.revision
    client = TestClient(create_app(FakeBundles(bundle)))

    response = client.get("/bundles/hello-saas/bundle.tar.gz")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{revision}"'

    with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
        names = tar.getnames()
    assert "authz.rego" in names
    assert ".manifest" in names  # OPA reads roots and revision from it


def test_unchanged_revision_answers_304():
    """A steady state costs one cheap request per poll, not a tarball."""
    bundle = stored("hello-saas", {"a.rego": "package poseidon\n"})
    revision = bundle.revision
    client = TestClient(create_app(FakeBundles(bundle)))

    response = client.get(
        "/bundles/hello-saas/bundle.tar.gz", headers={"If-None-Match": f'"{revision}"'}
    )
    assert response.status_code == 304


def test_torn_down_company_serves_its_tombstone():
    """Teardown revokes through the same channel policy arrived by: the
    served bundle is a tombstone, and OPA's next poll activates the
    nothing it contains."""
    tarball, revision = tombstone(["poseidon/authz/hello_saas"])
    bundles = FakeBundles(
        StoredBundle("hello-saas", revision, tarball, datetime.now(UTC))
    )
    client = TestClient(create_app(bundles))

    response = client.get("/bundles/hello-saas/bundle.tar.gz")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{revision}"'
    with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
        assert tar.getnames() == [".manifest"]


def test_unknown_company_is_404():
    client = TestClient(create_app(FakeBundles()))
    assert client.get("/bundles/nobody/bundle.tar.gz").status_code == 404
