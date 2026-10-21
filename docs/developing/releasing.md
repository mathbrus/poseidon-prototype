# Releasing

Poseidon is released as one thing. One lockfile, one Dockerfile for
every image, one chart that deploys them together, one acceptance suite
that proves the whole: a version per package would be fourteen numbers
nobody could act on independently. So there is **one version**, the git
tag, spelled in the tree exactly once, in the root `pyproject.toml`. The
package pyprojects, the chart's `version` and `appVersion` and `uv.lock`
repeat it only because their formats demand a number; `scripts/version.py`
keeps those copies equal and CI refuses a tree where they drift.

## What a release is

An annotated tag `vMAJOR.MINOR.PATCH+YYYY.MM.DD` on master, the matching
section of [the changelog](../changelog.md), and a GitHub release that
carries that section as its notes. The date is the day of tagging; it
rides as semver build metadata, so `v0.1.0+2026.10.21` still orders as
`0.1.0` and is also a valid PEP 440 local version and a valid chart
version.

## The bump rule

Before 1.0:

- **minor** when the unreleased delta adds, changes or removes a
  blueprint field, a CLI command or flag, a console route, a chart value,
  a policy rule or a prompt the company reads. A minor may break an
  applied blueprint; the changelog says so under *Changed*.
- **patch** when the delta is fixes, refactors, docs and tests only.
- **major** is `1.0.0`: a human's decision, never proposed.

`scripts/release-status.sh` classifies the delta by path:

| Surface | Paths |
|---|---|
| the blueprint and its policy | `packages/blueprint/src`, `packages/compiler/src`, `crds`, `examples` |
| the CLI | `packages/cli/src` |
| the console | `packages/console/src` |
| the chart | `deploy/helm` |
| the prompts | `packages/agent/src/agent/prompts` |

## When a release is proposed

The unit of change is a pull request merged to master; merging one never
implies a release. Each time one merges, Claude runs
`scripts/release-status.sh` and reads the facts it prints: the last
release and its date, the pull requests merged since, the surfaces they
touched, the changelog's *Unreleased* section and any merged pull request
that section leaves out.

If a surface was touched, Claude proposes a release in one message: the
version with the bump rule applied, today's date, the changelog section
as it would be published, and the one command that makes it. It never
tags, pushes a tag or creates a GitHub release without the human's
explicit yes in reply to that proposal. If no surface was touched, it
says so in one line and the delta accumulates. Anyone can ask for the
same assessment at any time with "release?".

## The act

```bash
scripts/release.sh 0.1.0+2026.10.21      # on master, after the yes
```

The script refuses off master, with a dirty tree or one that differs
from `origin/master`, with a version that is malformed, not dated today
or not above the last release, with an empty *Unreleased*, and until CI
is green on the commit being released. Then it stamps the version into
every mirror, turns *Unreleased* into the release's section, commits
directly on master, tags, pushes both, and creates the GitHub release.

That commit is the one kind of commit master takes without a pull
request: the review happened in the pull requests it releases, the
commit is mechanical, and the human's yes is its authorization.

## The changelog

`CHANGELOG.md` at the repository root, newest first, with *Added*,
*Changed*, *Fixed* and *Removed* under each release as needed. Every pull
request adds one plain-language line under *Unreleased* before it is
reported complete, ending with its number; `release-status.sh` names any
merged pull request the section does not mention. The docs site renders
the same file as its [Changelog](../changelog.md) page.

## The running version

Images carry the version: `deploy/build-images.sh` stamps the tree's
`git describe` against the release tags into `POSEIDON_VERSION` and the
OCI `org.opencontainers.image.version` label. A release's images answer
the tag's name; a build between releases answers the tag plus its
distance and commit (`v0.1.0+2026.10.21-5-gabc1234`, `-dirty` for an
uncommitted tree). The console reports it on `/healthz`, `poseidon
version` prints it beside the CLI's own, and `kind-up` ends by naming the
version the cluster answers. A pod still running an older build names
itself.
