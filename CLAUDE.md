# Poseidon

An autonomous company, run by cloud agents, that cannot
exceed its authority.

The company is the product. It lives in the cloud, stays up, and works
towards its vision for as long as that vision stands. A human writes the
vision and the hard limits (what may never happen, what must be approved,
how much may be spent). They do not drive the next step: the company
works out its own strategy and its own next slice of work. What they do
to a running company is bounded to the console's acts: answer a gate,
hold or release the loop, start or cancel a run, delete it.

## What a company is

A control loop with memory: observe the world, decide what is worth
doing, do it, learn, repeat. Not an org chart. Not a ticket queue. Not a
CI pipeline with a nicer brain.

- **Autonomy** — the system chooses the next action. Humans declare gates. 
  They do not stitch phases.
- **Continuity** — the system remembers.
- **Coordination** — many workstreams, shared artifacts, real
  priorities. One agent filing one ticket is not an organization.
- **Actuation** — the world is customers, money, mail, and code. A
  company that can only open a pull request is a workshop.
- **Quality** — permission is the easy third of governance. The hard
  two are whether the work was worth doing and whether it is actually
  good. Never let the agent grade its own homework as the gate of
  record.
- **Authority** — every consequence is decided by policy a human can
  read, diff, and revoke. Injection is assumed; containment is
  positional. Spend is a gate, not a warning: an exhausted budget parks
  the run until a human answers.

## Direction

The destination is far. Judge a change by whether it shortens the
distance, not by whether it arrives.

Closer means: more of the loop runs without a human starting the next
step; more of the world is reachable under policy; more memory and
coordination survive a restart — and authority is not looser than
before.

A change that only furnishes the cage, or that buys capability by
weakening the charter, is the wrong direction even if it ships.

## The tree is not the charter

This file is the source of truth. The tree is not always caught up —
it lags the charter between releases, and leftovers from earlier ones
remain. Old names, old control flow, old assumptions.

When you find something incoherent with this charter, stop and ask.
Do not paper over it, do not reinterpret it into the new world, do
not silently delete it. We will sort it out together.

## Glossary

One name per concept, everywhere: code, comments, docs, tests, CRs.
Check new names against this table; if a concept is missing, add it here
first.

<!-- --8<-- [start:glossary] -->
| Term | Meaning | Not |
|---|---|---|
| **role** | a catalog entry: tool ceiling, spawn authority, the procedures it may run — `pm`, `implementer`, `reviewer`, `contract-author`, `steward` | role template, executor, engineer, sdet, author, tier |
| **crew** | a named list of roles fulfilled together for one run (`feature-delivery`, `feature-review`, `contract-authoring`, `stewardship`); `crew_template` names it in policy input and registry rows | — |
| **agent** | one principal: one pod, one ServiceAccount, one registry row; standing (`<standing_name>-<n>`) or ephemeral (`<crew>-<role>-<run_id>`) | brain host, member, instance, participant (the `run_participants` table and its API keep the name; prose says ephemeral agent) |
| **agent host** | the process in the agent image: the dispatch HTTP API plus the SDK loop that runs procedures; the `agent-host` package, image `poseidon-agent-host` | brain host, runner, agent (the principal, not the code it runs) |
| **standing name** | the hire's name: the key under `standing:`, the Deployment/Service name | standing role |
| **conductor** | the one standing agent the company's runs are requested under, named once at `heartbeat.conductor`; at runtime `{agent_id, role, url}` | — |
| **procedure** | the agent host's unit of work: `scope`, `decide`, `implement`, `deliver`, `review` | mode, runner, phase, loop, runtime |
| **dispatch** | the orchestrator asking an agent to run a procedure on a text | — |
| **brain** | the model behind the LLM gateway, nothing else (mock brain, paid brain); the mock brain is the `mock-brain` package, image `poseidon-mock-brain` | mockllm |
| **workflow** | a Temporal workflow the heartbeat may start: `scope`, `implement`, `review`; plus `heartbeat` itself | family, intake |
| **task** | the text an implement or review run works: a ticket, a pull request | goal, brief |
| **observation** | the platform-composed reading of the world a conductor decides or scopes on: the documents, the open tickets, the delivered pull requests, the recent runs, capacity and spend | goal, prompt, context |
| **blueprint** | the YAML document a human applies; the charter is what it declares; validated against the `blueprint` package's JSON Schema | spec file, company file, schema (the package's old name) |
| **charter** | the blueprint a human applies: the vision, the budgets, the gates, the policies; this file is the charter of the project, not of a company | mission, constitution, goal file |
| **vision** | the human's statement of purpose in the charter, `company.vision`; reaches every procedure but `deliver` through the static prompt; changed only by applying the blueprint again | mission, goal, purpose |
| **bounds** | the charter's gates and limits as they bind this company's work — repository, branches, denied paths, contract tree, documents, merge — rendered once from the applied spec by `schema.bounds.render_bounds`, carried by every dispatch into the procedure's system prompt, trusted like the vision | rules, guardrails, constraints, policy summary |
| **strategy** | the document at `company.documents.strategy` on `base_ref`; the steward writes it once from the vision; no current role may change it afterwards (a future observer would) | roadmap, plan |
| **architecture masterplan** | the document at `company.documents.architecture` on `base_ref`; the steward writes it at bootstrap and revises it only to fulfil an architecture request; `architecture` in identifiers | ADR, design doc, `AGENTS.md` |
| **architecture request** | a proposal an ephemeral agent records with `architecture.request`; the platform turns it into a stewardship ticket | change request, RFC |
| **bootstrap ticket** | the ticket the platform files for a document absent on `base_ref` (`Bootstrap: strategy`, `Bootstrap: architecture masterplan`); a stewardship crew fulfils it | seed |
| **steward** | the catalog role that writes the two documents; ephemeral only; its crew is `stewardship` | librarian, architect |
| **observer** | a future standing hire that would revise the strategy; not in the catalog; named in comments and docs only | — |
| **profile** | the deployment's activity-bound sizing (`TIMEOUT_PROFILE`: `default` for a paid brain, `fast` for the mock), learned by every workflow from its first config activity | fast timeouts, timeouts on the input |
| **console** | the human's chokepoint: the one service a person acts on a company through (apply the blueprint, resolve a gate, start or cancel a run, wake the heartbeat, delete the company) and reads it through; verifies the human, asks policy, acts, audits — nothing else; the `poseidon` CLI is its client | API, front door, dashboard, operator gateway |
| **company namespace** | the one fixed Kubernetes namespace a cluster's company lives in (`common.topology.COMPANY_NAMESPACE`, `poseidon-company`): the reconciler creates it from the Company CR, the chart's NetworkPolicies select it, the worker puts sandboxes and ephemeral agents in it; it carries no company name | a namespace named after the company, tenant namespace |
| **human** | a verified person: on kind a ServiceAccount `human-<name>` the chart declares under `console.humans`, elsewhere an identity provider's user; `lifecycle: human`, never a catalog role; `company.humans` names those who may act on a company | operator, user, approver, admin |
| **rationale** | the reason an actor states for a decision at the moment it commits the company to something, recorded in cleartext (`audit_events.rationale`, at most 1000 characters) beside the decision's row; owed by every commitment, by no refusal — the rule is the reason; the CLI asks for it as `--reason` | reason (as a field name), note, payload, summary, rule (why policy answered) |
| **verdict** | a human's answer to a gate (`approved` or not), recorded by the console under the human's name before the workflow is signalled; the merge gate is released only on a recorded verdict | approval, signal, decision (a policy result) |
| **parked** | a run waiting on a gate for a human's verdict, in the API-visible state `parked-<gate>` (`parked-merge_gate`, `parked-budget_exhausted`); it waits the gate's `deadline_minutes`, escalates at half and expires at the end | paused, blocked, waiting, suspended |
| **pause** | the human act that holds the heartbeat: `heartbeat.pause` through the console, lifted by `heartbeat.resume` (`poseidon pause` / `poseidon resume`); a paused loop ticks, records `tick_paused` and starts nothing; runs in flight finish; not a stop — the company stays, deleting it is the one stop | freeze, halt, suspend, stop, sleep |
| **sleeping** | the heartbeat's state between ticks, waiting for a wake signal or the backstop cadence; `ticking` while a tick runs, `paused` while a human holds it; prose may say asleep | idle (a tick's outcome, not the loop's state), dormant, waiting, frozen |
| **halted** | a start the heartbeat refuses on a gate nobody resolves, recorded as the process-gate outcome `halted` (`gate.heartbeat_daily_cap`, `gate.heartbeat_bootstrap_exhausted`); lifted by the world changing (tomorrow's cap, a fulfilled bootstrap), never by a verdict; the loop keeps ticking | stopped, paused, parked, blocked |
| **cap** | a budget's number in the charter (`budgets.per_run.usd`, `heartbeat.daily_cap.usd`): the LLM gateway refuses a model call once the run's ledger SUM reaches the per-run cap, and the run parks; the heartbeat halts starts once the day's spend reaches the daily cap | hard stop, limit (a node's sizing), ceiling, budget (the section, not the number) |
| **reconciler** | the Kubernetes controller that converges `Company` and `AgentInstance` into the company namespace and writes `converged`; the `reconciler` package, image `poseidon-reconciler` | operator, controller |
| **converged** | the Company phase the reconciler writes once OPA serves the stored bundle revisions, the platform's and the company's; `activating` until then | ready, applied, installed |
| **tombstone** | the empty bundle over a torn-down company's roots that the bundle server keeps serving, so OPA's next poll erases its rules | deleted bundle, 404 |
<!-- --8<-- [end:glossary] -->

## Working rules

- **A company always has a heartbeat**: a blueprint without one does not
  compile, work originates only from its ticks, and stopping the company
  is deleting it. Pausing it (`poseidon pause`) holds the heartbeat and
  nothing else: runs in flight finish, the company stays. There is no
  human-started mode and no goal file.
- **LLM cost guardrail**: the mock LLM is the default for e2e runs.
  Never set `REAL_LLM=1` or use a paid provider without explicit
  opt-in from the user. The harness enforces this: the e2e suite
  and `scripts/acceptance.sh` refuse a paid brain (read from the
  deployed `llm-proxy` image, never from `.env`) unless
  `POSEIDON_ALLOW_PAID=1` is set — that variable is the opt-in
  spelling, and setting it still requires the user's say-so.
- **Docs**: `docs/` is the MkDocs site (`uv run mkdocs build`, strict, a
  CI gate); reference tables there are rendered from the code by
  `scripts/gen_docs.py`, never typed. The root README, this file and
  `packages/*/README.md` stay GitHub's, next to the code.
- **Tests**: run pytest per-package, not from the repo root. The full
  acceptance suite is `scripts/acceptance.sh`; `test_acceptance.py` is its
  index and capstone.
- **Responses API surface**: llm-gateway and mock-brain both speak the
  OpenAI Responses API; the agent's model table (`agent.models`) picks
  it per model family. Any new LLM API surface must land in both
  services.
- **Cluster**: the target environment is kind. Beware that `kind-up`
  can fill the disk, and that `:latest` image tags can leave stale
  pods running old code — check what's actually deployed.
- **"Converged"** for the control plane includes OPA bundle
  activation, not just pods being Ready.
- **Comments**: write them for a reader who does not know the
  implementation plan. Never mention phases, contract IDs (C2, C4,
  …), or how the code used to work. If you find such comments, strip
  them as you touch the file — they are debris, not documentation.
- **Leftovers**: if code, docs, or names contradict the charter above,
  ask immediately. Do not guess which world they belong to.
- **Releases**: Poseidon has one version, the git tag
  `vMAJOR.MINOR.PATCH+YYYY.MM.DD`, spelled once in the root
  `pyproject.toml` (`scripts/version.py` keeps the mirrors equal). After
  a pull request merges to master, run `scripts/release-status.sh` and,
  if a user-visible surface changed since the last tag, propose the next
  release per `docs/developing/releasing.md`: version, date, changelog
  section, the one command. Never tag, push a tag or create a GitHub
  release without the human's yes to that proposal. The release commit
  is the one direct commit master takes.
- **Changelog**: every pull request adds its line under `Unreleased` in
  `CHANGELOG.md`, ending with its number, before it is reported complete.
- **Configuration tiers**: a knob lives in exactly one place. Would a
  human revoke it with a diff? Blueprint. Does it name where something
  runs, which image, which secret, which model? Chart values, spelled
  once. Must two components agree on it? One constant in `common`.
  Nothing is "configurable in principle": a setting no template sets is
  a constant or gone, and a blueprint field nothing reads is gone.
