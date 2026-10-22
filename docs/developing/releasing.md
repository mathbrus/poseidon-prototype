# Releasing

Poseidon is released as one thing, with one version. This page explains
how a release is proposed and made.

## To cut a release

1. On an up-to-date `master`, see what changed since the last release:

    ```bash
    scripts/release-status.sh
    ```

2. Pick the version with the [bump rule](#the-bump-rule) and today's date,
   for example `0.2.1+2026.10.22`.
3. Make the release:

    ```bash
    scripts/release.sh 0.2.1+2026.10.22
    ```

The script stamps the version, turns *Unreleased* into the release's
changelog section, commits on `master`, tags, pushes, and creates the
GitHub release.

## What a release is

An annotated tag `vMAJOR.MINOR.PATCH+YYYY.MM.DD` on `master`, the matching
section of [the changelog](../changelog.md), and a GitHub release that
carries that section as its notes.

- The date is the day of tagging. It is semver build metadata, so
  `v0.1.0+2026.10.21` still orders as `0.1.0`. It is also a valid PEP 440
  local version and a valid chart version.
- The version is spelled once, in the root `pyproject.toml`. The package
  `pyproject.toml` files, the chart and `uv.lock` repeat it because their
  formats need a number. `scripts/version.py` keeps the copies equal, and
  CI refuses a tree where they differ.

One lockfile, one Dockerfile, one chart and one acceptance suite cover the
whole system, so a version per package would mean nothing on its own.

## The bump rule

Before 1.0:

- **minor** when the changes add, change or remove a blueprint field, a
  CLI command or flag, a console route, a chart value, a policy rule, or a
  prompt the company reads. A minor may break an applied blueprint, and
  the changelog says so under *Changed*.
- **patch** when the changes are fixes, refactors, docs and tests only.
- **major** is `1.0.0`, decided by a person, never proposed.

`scripts/release-status.sh` sorts the changes by the paths they touch:

| Surface | Paths |
|---|---|
| the blueprint and its policy | `packages/blueprint/src`, `packages/compiler/src`, `crds`, `examples` |
| the CLI | `packages/cli/src` |
| the console | `packages/console/src`, `packages/common/src/common/console` |
| the chart | `deploy/helm` |
| the prompts | `packages/agent-host/src/agent_host/prompts` |

It also says how the console interface changed since the last release,
by comparing the committed OpenAPI snapshot
(`packages/console/openapi.json`) at both ends:
`breaking` (a route or field removed, a field retyped, a request field
newly required, an enum narrowed), `additive`, or `unchanged`. A
breaking change is what every console client must follow, so the
proposal's changelog says it under *Changed*. There is no separate
interface version: the release is the version.

## When a release is proposed

Merging a pull request never makes a release. After each merge, the
maintainer runs `scripts/release-status.sh`. It prints the last release,
the pull requests merged since, the surfaces they touched, the
*Unreleased* section, and any merged pull request the section leaves out.

- If a surface was touched, the maintainer proposes a release: the
  version, the date, the changelog section as it will be published, and
  the command. Nothing is tagged, pushed or published until someone with
  release rights says yes.
- If no surface was touched, the changes wait for the next release.

## What the script refuses

`scripts/release.sh` refuses to run:

- off `master`, with uncommitted changes, or with a `master` that differs
  from `origin/master`;
- with a malformed version, a date other than today, or a version not
  above the last release;
- with an empty *Unreleased* section;
- until CI is green on the commit being released.

The release commit is the one commit `master` takes without a pull
request. The review happened in the pull requests it releases, and the
commit itself is mechanical.

## The changelog

`CHANGELOG.md` at the repository root, newest first, with *Added*,
*Changed*, *Fixed* and *Removed* under each release as needed. Every pull
request adds one plain-language line under *Unreleased*, ending with its
number. The site shows the same file as its [Changelog](../changelog.md)
page.

## The running version

Every image carries the version. `deploy/build-images.sh` stamps the
tree's `git describe` into `POSEIDON_VERSION` and the OCI
`org.opencontainers.image.version` label. A build between releases names
the tag plus its distance and commit, for example
`v0.1.0+2026.10.21-5-gabc1234`, with `-dirty` for uncommitted changes.

To see what is running:

- `poseidon version` prints the CLI's version and the console's.
- The console reports its version on `/healthz`.
- `kind-up.sh` ends by naming the version the cluster runs.
