"""The bundle server: standard OPA bundle semantics over stored bundles."""

import io
import tarfile
from datetime import UTC

from fastapi.testclient import TestClient

from poseidon_operator.bundleserver import create_app
from registry.bundles import pack


class FakeBundles:
    def __init__(self) -> None:
        self.stored: dict[str, object] = {}

    def put(self, company: str, files: dict[str, str]) -> str:
        from datetime import datetime

        from registry.bundles import StoredBundle

        tarball, revision = pack(files)
        self.stored[company] = StoredBundle(
            company=company,
            revision=revision,
            tarball=tarball,
            updated_at=datetime.now(UTC),
        )
        return revision

    def get(self, company: str):
        return self.stored.get(company)

    def delete(self, company: str) -> None:
        self.stored.pop(company, None)


def test_bundle_is_served_as_a_tarball_with_its_revision():
    bundles = FakeBundles()
    revision = bundles.put("hello-saas", {"authz.rego": "package poseidon.authz\n"})
    client = TestClient(create_app(bundles))

    response = client.get("/bundles/hello-saas/bundle.tar.gz")
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{revision}"'

    with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
        names = tar.getnames()
    assert "authz.rego" in names
    assert ".manifest" in names  # OPA reads roots and revision from it


def test_unchanged_revision_answers_304():
    """A steady state costs one cheap request per poll, not a tarball."""
    bundles = FakeBundles()
    revision = bundles.put("hello-saas", {"a.rego": "package poseidon\n"})
    client = TestClient(create_app(bundles))

    response = client.get(
        "/bundles/hello-saas/bundle.tar.gz", headers={"If-None-Match": f'"{revision}"'}
    )
    assert response.status_code == 304


def test_changed_policy_changes_the_revision():
    bundles = FakeBundles()
    first = bundles.put("hello-saas", {"a.rego": "package poseidon\n"})
    second = bundles.put("hello-saas", {"a.rego": "package poseidon\n# edit\n"})
    assert first != second


def test_torn_down_company_serves_nothing():
    """Teardown revokes: OPA stops being handed rules for a company that no
    longer exists."""
    bundles = FakeBundles()
    bundles.put("hello-saas", {"a.rego": "package poseidon\n"})
    bundles.delete("hello-saas")
    client = TestClient(create_app(bundles))
    assert client.get("/bundles/hello-saas/bundle.tar.gz").status_code == 404


def test_unknown_company_is_404():
    client = TestClient(create_app(FakeBundles()))
    assert client.get("/bundles/nobody/bundle.tar.gz").status_code == 404
