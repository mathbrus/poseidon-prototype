# Contributing

## Repository map

```
packages/          thirteen workspace packages (see Packages)
docs/              this site's pages; mkdocs.yml at the root builds it
scripts/           acceptance.sh, gen_docs.py
Dockerfile         one recipe for every image; deploy/build-images.sh is the matrix
deploy/helm/       the Poseidon chart, plus Temporal's values
deploy/kind/       kind bootstrap, teardown, .env
crds/              Company and AgentInstance CustomResourceDefinitions
examples/          hello-saas.yaml and hello-saas-delegated.yaml (one company, two profiles), pirate-game.yaml (another company)
tests/e2e_kind/    the acceptance suite that runs against a live cluster
```

## Tests run per package, never repo-wide

```bash
uv sync
uv run pytest packages/common
for p in packages/*/; do uv run pytest "$p" -q; done   # all thirteen suites, no cluster needed
```

Every package has its own `tests/conftest.py`, and pytest cannot collect
several modules with the same basename into one rootdir — `uv run pytest
packages/` fails at collection, and that is expected, not a broken
checkout. `packages/registry` reports every test skipped without
`REGISTRY_DSN`; that is green: those tests exercise real SQL, so they need
a real Postgres, and a fake would prove nothing.

The end-to-end suite is a separate thing, run against a live cluster:
[Run the acceptance suite](../guides/acceptance.md).

## The golden files

`packages/compiler/tests/golden/` holds the compiled plan and Rego bundle
for the example blueprint. **They are generated. Never edit them by hand.**

After any change to the generator — including the text of a comment the
compiler emits into Rego:

```bash
uv run python -m compiler.regolden
uv run pytest packages/compiler
git diff packages/compiler/tests/golden/   # read it; confirm it matches intent
```

Regolden is deliberately separate from the test run, so updating a golden is
an explicit act whose diff a human reads.

## Lint and format

```bash
uv run ruff check .
uv run ruff format --check .
```

## The docs site

```bash
uv sync --group docs
uv run mkdocs serve        # http://127.0.0.1:8000, rebuilds on save
uv run mkdocs build        # what CI runs; strict, so a broken link fails
```

The reference pages are rendered from the code at build time by
`scripts/gen_docs.py`: a page carries an HTML comment naming a generator
(`gen: roles`, `gen: tools:sandbox`, …) where a table belongs, and the
script fills it from the catalogs, the gateway's tool
definitions, the CLI, the console's route table and the workflow
classes. Nothing rendered is
committed. Add a tool without a docstring, or drop a marker, and the build
says so.

The pages under `docs/` are the site's; the root `README.md`, `CLAUDE.md`
and each package's `README.md` are GitHub's and stay next to the code. The
one text shared between the two is the glossary, which the site includes
from `CLAUDE.md` rather than copying.

## CI

| Job | Gate |
|---|---|
| `lint` | ruff check + format |
| `lock` | `uv lock --check`; advisory `pip-audit` |
| `helm` | chart lints and renders — and a bare render must **fail**, proving secrets are required |
| `unit` | all thirteen suites, with a real Postgres so the registry tests run |
| `opa` | `python -m compiler` compiles the example blueprint with no cluster; the bundle parses under the pinned OPA |
| `docs` | the site builds strict; the built site is the run's artifact |
| `images` | all nine build; images resolve locked versions; no secret-shaped env baked in; every image runs as uid 10001 |
| `e2e-kind` | the full acceptance suite — **manual dispatch only** (real PRs, real cluster) |
