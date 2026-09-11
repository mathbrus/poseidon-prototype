"""Bundle packing: deterministic tarballs whose roots cover exactly what
they contain. Pure functions — no database needed."""

import io
import json
import tarfile

import pytest

from registry import pack, tombstone


def manifest_of(tarball: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        return json.loads(tar.extractfile(".manifest").read())


def test_roots_come_from_the_package_declarations():
    """OPA bundle activation erases everything under the roots that the
    bundle does not supply. Company packages are per-company and the
    platform package is its own bundle; a root any wider (say, a bare
    `poseidon`) would erase whatever another bundle owns on the first
    poll and take every gateway decision with it."""
    tarball, _revision = pack(
        {
            "authz.rego": "package poseidon.authz\n\nimport rego.v1\n",
            "role_pm.rego": "# comment first\npackage poseidon.authz\n",
        }
    )
    manifest = manifest_of(tarball)
    assert manifest["roots"] == ["poseidon/authz"]


def test_distinct_packages_become_distinct_roots():
    tarball, _ = pack(
        {
            "a.rego": "package poseidon.authz\n",
            "b.rego": "package poseidon.meta\n",
        }
    )
    assert manifest_of(tarball)["roots"] == ["poseidon/authz", "poseidon/meta"]


def test_a_file_without_a_package_is_refused():
    """A package-less file would land under an unclaimed root — refuse it
    rather than ship a bundle whose ownership is undefined."""
    with pytest.raises(ValueError, match="no package declaration"):
        pack({"broken.rego": "# no package here\n"})


def test_identical_files_pack_identically():
    """The revision is content-addressed and the tar metadata pinned, so an
    unchanged blueprint is a 304 for OPA, not a churned revision."""
    files = {"authz.rego": "package poseidon.authz\n"}
    first_tar, first_rev = pack(files)
    second_tar, second_rev = pack(files)
    assert (first_tar, first_rev) == (second_tar, second_rev)
    assert manifest_of(first_tar)["revision"] == first_rev


def test_a_tombstone_declares_its_roots_and_nothing_else():
    """Revocation is a bundle too: the roots it claims, no files under
    them, so activation erases what the previous revision supplied."""
    tarball, revision = tombstone(["poseidon/authz/hello_saas"])
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        assert tar.getnames() == [".manifest"]
    manifest = manifest_of(tarball)
    assert manifest == {"revision": revision, "roots": ["poseidon/authz/hello_saas"]}


def test_a_tombstone_is_its_roots():
    """Same roots, same revision — a re-run teardown is a 304 for OPA;
    other roots, another revision."""
    _, first = tombstone(["poseidon/authz/hello_saas"])
    _, again = tombstone(["poseidon/authz/hello_saas"])
    _, other = tombstone(["poseidon/authz/other_corp"])
    assert first == again != other


def test_a_rootless_tombstone_is_refused():
    """No roots means the whole tree: activating it would erase the
    platform bundle along with the company's."""
    with pytest.raises(ValueError, match="roots"):
        tombstone([])
