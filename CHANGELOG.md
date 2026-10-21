# Changelog

Every release of Poseidon, newest first. Poseidon has one version: the
git tag `vMAJOR.MINOR.PATCH+YYYY.MM.DD` is the release, the section
below it is the release's story, and the GitHub release carries that
section as its notes. A pull request merged to master adds its line
under *Unreleased* before it is reported complete, ending with its
number; `scripts/release.sh` turns *Unreleased* into the next section.
How a release is decided is the Releasing page of the docs site
(`docs/developing/releasing.md`).

## [Unreleased]

- The platform closes the tickets a merged pull request fulfilled — a rework
  chain's all at once, hand merges included — and the pull request a rework
  ticket supersedes, through the platform-only `issues.close` and
  `git.close_pr`, each with its rationale on the audit row (#79)

## [0.1.0+2026.10.21] - 2026-10-21

The first numbered release: everything since the `poc` tag (2026-07-21),
grouped by theme rather than listed merge by merge.

### Added

- The company documents: a vision the human writes, a strategy the
  steward writes once, an architecture masterplan it revises on request;
  bootstrap tickets for a document absent on the base ref; architecture
  requests from ephemeral agents; no company without a heartbeat (#48,
  #50).
- The heartbeat: the standing loop that observes the world, decides and
  starts the company's runs; it wakes on the company's own state changes
  and reconciles on a timer; an aborted run does not consume its work
  item forever (`max_attempts`); a delivered pull request's live state is
  read before it is offered for review; the company works overnight
  (#10, #29, #31, #35).
- The merge gate of record: a pull request merges under policy, delegated
  by path class, with rework tickets up to `max_reworks` and a human's
  verdict beyond; the split oracle, a contract tree the implementer
  cannot rewrite, with a contract-authoring crew (#12, #14).
- The console, the one human chokepoint: verified humans named in the
  blueprint, verdicts recorded before the workflow is signalled, the
  Temporal door closed; `poseidon login` (#52).
- The `poseidon` CLI as the human's window: `blueprint new`, `preview`
  and `apply` (which waits for the company to converge), `company show`
  and `delete`, `runs`, `run story`, `follow`, `verdict`, `heartbeat`,
  `pause` and `resume`; one company per cluster, in a fixed namespace;
  converged means OPA serves the revision (#55, #56, #58, #67, #74).
- The bounds: every procedure learns where work may go, rendered once from
  the applied blueprint; a denial names what it refused (#71).
- Real cost: the gateway meters what LiteLLM reports and fails closed
  without it; a model table by family picks the API per model (#37, #62).
- A real model behind the LLM gateway, the mock brain for everything
  else; implement runs start from a GitHub issue; the PM leaves its
  scoping summary on each ticket it files (#6, #8, #27).
- Audit event names spelled once, in `common.audit` (#60).
- The docs site, built strict in CI, its reference tables rendered from
  the code (#46).
- Releases: one version for the whole of Poseidon, spelled once and
  checked in CI; this changelog; `scripts/release-status.sh` and
  `scripts/release.sh`; images stamped with the version, the console
  reporting it, `poseidon version`, and `kind-up` naming the version it
  started (#77).

### Changed

- One vocabulary everywhere: standing and ephemeral agents, procedures,
  one role axis, conductor and heartbeat, pause as a hold; the glossary
  in `CLAUDE.md` is the source of truth (#21, #25, #41, #74).
- Configuration: a knob lives in exactly one place, blueprint, chart
  values or a constant in `common` (#43).
- The agent host uses more of the OpenHands SDK without loosening
  authority (#23).
- The acceptance suite trimmed to one flow per claim, its harness made a
  contract of record; repo-wide cleanups (#19, #39).
- Teaching-grade READMEs, a README that leads with the product and the
  vision, a commissioned critique and the removals it asked for (#3, #4,
  #5, #16, #17).

### Fixed

- A repository without tests can merge its first pull request (#64).
- `kind-up` with a paid brain could not read the LiteLLM image (#28); the
  chart-vs-topology check runs with PyYAML available (#44).

### Removed

- The planner; the platform JWT; `per_role_monthly`; the default
  Temporal web UI (#7, #41).

The MVP that all of this builds on, everything a company needed to run
its first pull request through governance on kind, landed as #2.
