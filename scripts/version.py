"""The one version of Poseidon, and its mirrors.

Poseidon is released as one thing: one lockfile, one Dockerfile for every
image, one chart that deploys them together, one acceptance suite that
proves the whole. So it has one version, spelled once, in the root
`pyproject.toml`; the git tag `v<version>` names the release. The package
pyprojects, the chart (`version` and `appVersion`) and `uv.lock` repeat
the number only because their formats demand one. This script keeps the
copies equal, and CI refuses a tree where they drift.

    uv run python scripts/version.py show         print the version
    uv run python scripts/version.py check        every mirror equals it; exit 1 naming each drift
    uv run python scripts/version.py set VERSION  rewrite the version and every mirror, refresh uv.lock

A version is `MAJOR.MINOR.PATCH+YYYY.MM.DD`: semver with the release day
as build metadata, which is also a valid PEP 440 local version and a
valid Helm chart version. `scripts/release.sh` is the only caller of
`set`.
"""

import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_PYPROJECTS = sorted(ROOT.glob("packages/*/pyproject.toml"))
CHART = ROOT / "deploy/helm/poseidon/Chart.yaml"
LOCK = ROOT / "uv.lock"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\+\d{4}\.\d{2}\.\d{2})?$")
PYPROJECT_LINE = re.compile(r'^version = "(?P<value>[^"]+)"$', re.MULTILINE)
CHART_VERSION_LINE = re.compile(r"^version: (?P<value>\S+)$", re.MULTILINE)
CHART_APP_LINE = re.compile(r'^appVersion: "(?P<value>[^"]+)"$', re.MULTILINE)


def current() -> str:
    """The version, from the one place it is declared."""
    with ROOT_PYPROJECT.open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def mirrors() -> Iterator[tuple[str, str]]:
    """Every copy of the version the tree carries, labelled, in file order."""
    for path in PACKAGE_PYPROJECTS:
        with path.open("rb") as f:
            yield str(path.relative_to(ROOT)), str(tomllib.load(f)["project"]["version"])
    text = CHART.read_text()
    chart = str(CHART.relative_to(ROOT))
    yield f"{chart} version", _one(CHART_VERSION_LINE, text, chart)
    yield f"{chart} appVersion", _one(CHART_APP_LINE, text, chart)
    with LOCK.open("rb") as f:
        lock = tomllib.load(f)
    for package in lock.get("package", []):
        editable = str(package.get("source", {}).get("editable", ""))
        if editable.startswith("packages/"):
            yield f"uv.lock {package['name']}", str(package["version"])


def _one(pattern: re.Pattern[str], text: str, where: str) -> str:
    matches = pattern.findall(text)
    if len(matches) != 1:
        sys.exit(f"{where}: expected exactly one {pattern.pattern!r} line")
    return matches[0]


def check() -> int:
    version = current()
    drift = [(label, value) for label, value in mirrors() if value != version]
    for label, value in drift:
        print(f"{label}: {value} != {version}", file=sys.stderr)
    if drift:
        print(
            f"{len(drift)} mirror(s) drift from {version}: "
            "`uv run python scripts/version.py set <version>` rewrites them all",
            file=sys.stderr,
        )
        return 1
    print(f"{version}: every mirror agrees")
    return 0


def set_version(version: str) -> None:
    if not VERSION_RE.match(version):
        sys.exit(f"{version!r} is not MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH+YYYY.MM.DD")
    for path in (ROOT_PYPROJECT, *PACKAGE_PYPROJECTS):
        _rewrite(path, PYPROJECT_LINE, f'version = "{version}"')
    _rewrite(CHART, CHART_VERSION_LINE, f"version: {version}")
    _rewrite(CHART, CHART_APP_LINE, f'appVersion: "{version}"')
    # The lockfile records every workspace member's version; uv rewrites
    # it from the pyprojects without touching the resolved dependencies.
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True)
    print(f"{version}: written to every mirror")


def _rewrite(path: Path, pattern: re.Pattern[str], line: str) -> None:
    text = path.read_text()
    _one(pattern, text, str(path.relative_to(ROOT)))
    path.write_text(pattern.sub(line, text, count=1))


def main(argv: list[str]) -> int:
    match argv:
        case ["show"]:
            print(current())
            return 0
        case ["check"]:
            return check()
        case ["set", version]:
            set_version(version)
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
