"""`python -m compiler BLUEPRINT [--out DIR]` — compile a blueprint with no
cluster and no registry: the plan as JSON on stdout, the bundles on disk
when asked. What CI and the docs build use; a human applies through the
console, which compiles against the live registry.
"""

import argparse
import json
import sys
from pathlib import Path

from compiler import EMPTY, compile_blueprint
from compiler.errors import CompileError
from schema import load_blueprint
from schema.validate import ValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m compiler")
    parser.add_argument("blueprint", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        help="Write the emitted Rego bundles here (one file per rule set)",
    )
    args = parser.parse_args(argv)
    try:
        plan = compile_blueprint(load_blueprint(args.blueprint), snapshot=EMPTY)
    except (ValidationError, CompileError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        # Clear stale rule files first: after a firing diff, a leftover
        # rule file would silently widen the bundle.
        for stale in args.out.glob("*.rego"):
            stale.unlink()
        files = {**plan.bundle.files, **plan.platform_bundle.files}
        for name, body in files.items():
            (args.out / name).write_text(body)
        print(f"wrote {len(files)} bundle files to {args.out}", file=sys.stderr)
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
