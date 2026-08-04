"""Regenerate the compiler's golden files.

    uv run python -m compiler.regolden

Separate from the tests on purpose: golden files are a review artifact, so
updating them is an explicit act whose diff a human reads, never something
a test run does quietly on the way past.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN = REPO_ROOT / "packages" / "compiler" / "tests" / "golden"


def main() -> None:
    from schema import load_blueprint

    from compiler import compile_blueprint

    plan = compile_blueprint(load_blueprint(REPO_ROOT / "examples" / "hello-saas.yaml"))
    GOLDEN.mkdir(parents=True, exist_ok=True)
    (GOLDEN / "hello-saas.plan.json").write_text(
        json.dumps(plan.as_dict(), indent=2, sort_keys=True) + "\n"
    )
    bundle_dir = GOLDEN / "hello-saas-bundle"
    bundle_dir.mkdir(exist_ok=True)
    for stale in bundle_dir.glob("*.rego"):
        stale.unlink()
    # Both bundles side by side — their packages are disjoint, and the
    # platform bundle is as much a review artifact as the company one.
    files = {**plan.bundle.files, **plan.platform_bundle.files}
    for name, body in files.items():
        (bundle_dir / name).write_text(body)
    print(f"wrote {GOLDEN}/hello-saas.plan.json and {len(files)} rego files")


if __name__ == "__main__":
    main()
