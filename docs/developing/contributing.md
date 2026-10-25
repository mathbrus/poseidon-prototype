# Contributing

This page gets you from a fresh clone to a merged pull request: where
things live, how to test, and what CI checks.

## Repository map

```text
packages/          thirteen workspace packages (see Packages)
docs/              this site; mkdocs.yml at the root builds it
scripts/           acceptance.sh, gen_docs.py, version.py, release-status.sh, release.sh
Dockerfile         one recipe for every image; deploy/build-images.sh builds them all
deploy/helm/       the Poseidon chart, and Temporal's values
deploy/kind/       kind bring-up and teardown, and .env.example
crds/              the Company and AgentInstance resource definitions
examples/          hello-saas.yaml and hello-saas-automated.yaml (one company, two profiles), pirate-fight.yaml
tests/e2e_kind/    the acceptance suite, run against a live cluster
```

## Your first pull request

1. Branch from `master`.
2. Make the change, with its tests.
3. Run the tests of every package you touched, and lint:

    ```bash
    uv run pytest packages/<package>
    uv run ruff check .
    uv run ruff format --check .
    ```

4. If you changed the docs, build the site: `uv run mkdocs build`.
5. Add one line under *Unreleased* in `CHANGELOG.md`, ending with the pull
   request's number.
6. Open the pull request. CI runs the checks below.

## Run the tests

Run the tests one package at a time:

```bash
uv sync
uv run pytest packages/common                          # one package
for p in packages/*/; do uv run pytest "$p" -q; done   # all thirteen, no cluster needed
```

`uv run pytest packages/` fails at collection, and that's expected. Every
package has its own `tests/conftest.py`, and pytest can't collect several
modules with the same name into one run.

The registry's tests that read or write a database need a real Postgres.
Without `REGISTRY_DSN` they skip, and the run is still green. CI runs them
against a real database. Point `REGISTRY_DSN` only at a database you can
throw away: the tests empty every table.

The acceptance harness has a few rules of its own that need no cluster:

```bash
uv run pytest tests --ignore=tests/e2e_kind
```

The end-to-end suite runs against a live cluster:
[Run the acceptance suite](acceptance.md).

## What a test proves

A test earns its run only if it fails when the claim in its name breaks.
Hold every test you write or touch to these rules.

- **A refusal names its rule.** Assert the rule that refused, not only that
  something was refused. Then show that the rightful caller, with the same
  input, is allowed. Without both, an unrelated refusal passes the test.
- **A bound comes from the code.** Check spend against the blueprint's
  cap, and retries against the blueprint's limit, never against a number
  the test makes up.
- **A read of history is fenced.** Take a mark before the act, and read
  only the audit rows after it. On the cluster, the mark is the database's
  own clock (`db_now()` in `tests/e2e_kind/cluster.py`).
- **A claim is proven once, at the cheapest level that can prove it.**
  The compiler's tests decide policy against a real OPA. The orchestrator's
  tests run the workflows. Each gateway's tests prove its own checks. The
  acceptance suite proves what only a cluster can: convergence, identity,
  egress, and the loop end to end.
- **Watch a new test fail.** Break the code it guards, check that the test
  fails, then put the code back.

## Update the golden files

`packages/compiler/tests/golden/` holds the compiled plan and policy bundle
for the example blueprint. They are generated, so never edit them by hand.
After any change to what the compiler emits, including a comment it writes
into Rego:

```bash
uv run python -m compiler.regolden
uv run pytest packages/compiler
git diff packages/compiler/tests/golden/   # read it, and check it matches your intent
```

Regenerating is a separate step from testing, so every change to a golden
file is a diff someone reads.

## Check the version

```bash
uv run python scripts/version.py check   # every copy of the version matches the root pyproject.toml
```

[Releasing](releasing.md) explains the one version and how a release is
made.

## Build the docs

```bash
uv sync --group docs
uv run mkdocs serve        # http://127.0.0.1:8000, rebuilds on save
uv run mkdocs build        # what CI runs: strict, so a broken link fails
```

[Writing docs](writing-docs.md) has the style rules and explains the
generated reference pages.

## CI

| Job | Checks |
|---|---|
| `lint` | `ruff check` and `ruff format --check`; the version's copies agree |
| `lock` | `uv lock --check`; an advisory `pip-audit` |
| `helm` | the chart lints and renders, and a render without secrets fails |
| `unit` | all thirteen package suites, with a real Postgres, and the acceptance harness's own rules |
| `opa` | the example blueprint compiles with no cluster, and the bundle loads in the pinned OPA |
| `docs` | the site builds strict; the built site is the run's artifact |
| `images` | all nine images build, install the locked versions, bake in no secret, and run as uid 10001 |
| `e2e-kind` | the full acceptance suite; manual dispatch only, because it opens real pull requests |
