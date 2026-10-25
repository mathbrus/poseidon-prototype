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

- Tests prove what they are named for, and run faster. A refusal test
  names the rule that refused and shows the rightful caller allowed; a
  spend or retry bound comes from the applied blueprint; a read of the
  audit history is fenced to the test's own window. The thirteen package
  suites run in about a minute instead of almost three (one OPA server
  per bundle, one Temporal test server per module), and the acceptance
  suite in about 53 minutes instead of 78: arcs that drove the same
  chain are folded, and the harness reports where its own waiting goes.
  *Changed:* the LLM gateway refuses a model call of a company with no
  positive per-run cap (403 `budget_cap_missing`) instead of letting it
  through unbounded; OPA polls its bundles every one to three seconds
  and the reconciler re-checks activation every second, so an applied
  blueprint reads `converged` sooner; `poseidon blueprint new` asks for
  "tries per ticket before it is abandoned" (#101)

## [0.5.0+2026.10.25] - 2026-10-25

- One name per concept for the company's document and for merging
  without a person. The company's document is the blueprint, never "the
  charter": the agent prompts introduce the bounds as "The blueprint as
  it binds this company's work", the console's limits and bounds
  descriptions say "blueprint", and `poseidon blueprint new` ends its
  header with "`poseidon blueprint apply` applies it to the company."
  A merge gate is automated when it has an `automate` block (its `paths`
  and `max_reworks`), and the bounds text and the review prompt say
  "automated". *Changed:* the merge gate drops `resolved_by` and
  `delegate`, and a blueprint carrying either is refused; the merge rules
  `merge_resolve_delegated` and `merge_resolve_undelegated_path` are now
  `merge_resolve_automated` and `merge_resolve_path_not_automated`, and
  `merge.resolve` takes `resolver: automated`; the gate outcomes
  `policy_resolved` and `policy_declined` are now `automated` and
  `automation_declined` (rows already written keep their names);
  `poseidon blueprint new` asks whether routine changes may merge without
  a person instead of who decides; `examples/hello-saas-delegated.yaml`
  is `examples/hello-saas-automated.yaml` (#97)
- Every way a ticket ends is recorded and shown on GitHub. A delivery
  whose pull request closes without merging is refused: the pull request
  closes with the reason on it (your rejection's words, an expired gate,
  a moved head, a red or conflicting merge onto a moved base, an
  abandoned review, a close on GitHub), its branch goes, and the ticket
  comes back as one spent attempt whose next delivery reads why. A ticket
  whose attempts are spent closes as not planned with its rework chain.
  The PM may dismiss a ticket the company should not work while it
  scopes, through `issues.dismiss`, within compiled limits. A review
  waiting on your merge verdict gives back its crew, its sandbox and its
  place under `max_concurrent_runs`, unless its pull request edits the
  contract tree. A merge onto a base that moved since the review is
  verified first. *Changed:* applying a blueprint with another
  `stack.repo.url` is refused while the company lives; `issues.close`
  takes `reason`, `git.close_pr` takes `delete_branch`, `repo.archive`
  takes `merge_onto`, and `git.read` answers `base_sha`; new rules
  `issues_dismiss_*`; the activity `record_attempts_exhausted` is
  `abandon_exhausted` (#98)
- Deleting a company erases everything the platform recorded about it:
  its runs, tickets, spend, audit rows (the delete's own included) and
  workflow histories. A company applied again under the same name starts
  with no history, and its daily cap starts at $0. The delete holds the
  heartbeat, ends the runs and waits for each, and answers once the
  company is gone. A delete that cannot finish in time says so and
  leaves the company held; a second delete finishes it.
  `poseidon company delete --purge-remote` also resets the repository in
  place: every open pull request and issue closed, every branch but the
  base and default ones deleted, and those forced to one empty root
  commit. The CLI asks for the repository's `owner/name` typed out, and
  `poseidon company show` names the repository. *Changed:*
  `company delete --no-wait` and `DeleteRequest.wait` are gone;
  `DeleteRequest` takes `purge_remote` and `CompanyDeleted` answers
  `purged_remote`; a delete is allowed under `company_delete` or
  `company_delete_purge_remote`, on `args.purge_remote`; the gateway
  tool `repo.purge` and its rule `console_repo_purge` are the console's
  alone; the tick outcome `stopped` is gone (#99)

## [0.4.0+2026.10.22] - 2026-10-22

- Nothing an agent does in its sandbox moves a platform check or what is
  delivered or merged. A review pins the pull request's head commit:
  every sandbox is built from it and the merge names it, so a push after
  the review read it fails the run (`head moved after review`) and leaves
  the PR open. Under a delegated merge gate the platform runs the tests
  before the reviewer is dispatched; a red suite files rework with no
  reviewer spent. `sandbox.git_bundle` makes the delivery commit of the
  workspace as it stands, authored by the calling agent, and `deliver`
  holds only `read_file` and `open_pr`; a delivery policy refuses on a
  path goes back to implement and is verified again, under
  `max_verify_retries`. Shell output reaches the model as plain text with
  each stream's tail kept, a read without a range returns at most 2,000
  lines, the opening message lists the workspace's files (`list_dir` is
  no longer a model tool) and the sandbox ships ripgrep. *Changed:*
  `poseidon blueprint new` no longer asks whether the reviewer may fix
  code, the example blueprints carry no `tools.reviewer` block,
  `git.merge_pr` takes `sha` and `sandbox.git_bundle` takes `message`
  (#86)

## [0.3.0+2026.10.22] - 2026-10-22

- The console's HTTP surface is one typed interface, `common.console`: a
  request and a response model per route, closed sets for every state,
  status and outcome, refusals as `Problem` or `PolicyRefusal`. Its
  OpenAPI snapshot and golden examples are committed and checked in CI,
  and `scripts/console_interface.py --breaking` says whether a change
  breaks clients. The CLI parses every answer into its model. New reads:
  parked runs with their deadlines, a run's crew, the standing agents,
  the tickets filed, the company's limits and bounds, the applied
  blueprint's text (`poseidon blueprint show`), the two documents
  through the MCP gateway, and what a human may do now (`GET /acts`,
  `poseidon acts`). *Changed:* request bodies say `rationale`, not
  `reason`; `companies.blueprint` is `compiled_spec`; a gate's park is
  recorded as `gate_parked`; a second start of a ticket or pull request
  a live run holds is refused (#85)

## [0.2.1+2026.10.22] - 2026-10-22

- The docs are rewritten for readers: a free Quickstart with real
  output, how-to guides and a troubleshooting page, explanation pages
  (How it works, The merge gate, How the heartbeat decides, Security
  model, Limitations) in place of the argument essay, and a generated
  reference (blueprint field descriptions, one CLI entry per command
  with examples, policy rules, environment variables, glossary); every
  package README on one template; American English, held by CI. Every
  `poseidon` command's `--help` ends with examples (#83)

## [0.2.0+2026.10.21] - 2026-10-21

- The LLM gateway *refuses* a model call at a run's cap and the run parks;
  nothing "hard-stops" (glossary row `cap`), and the compiler's
  `budgets.per_run` messages say so. Four packages take their glossary
  names, with their images and chart keys: `schema` → `blueprint`,
  `mockllm` → `mock-brain` (image `poseidon-mock-brain`,
  `values-mock-brain.yaml`), `agent` → `agent-host` (image
  `poseidon-agent-host`, chart `images.agentHost`), `operator` →
  `reconciler` (image and chart objects `poseidon-reconciler`, chart
  `images.reconciler`); a cluster is recreated, not upgraded in place (#81)

## [0.1.1+2026.10.21] - 2026-10-21

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
