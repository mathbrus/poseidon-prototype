# Poseidon

An autonomous software company, run by cloud coding agents, that cannot
exceed its authority — because the system will not let it, not because
the prompt asked nicely.

The company is the product. It lives in the cloud, stays up, and works
toward goals for as long as those goals remain. A human sets the charter
and the hard stops (what may never happen, what must be approved, how
much may be spent). They do not drive the next step.

## What a company is

A control loop with memory: observe the world, decide what is worth
doing, do it, learn, repeat. Not an org chart. Not a ticket queue. Not a
CI pipeline with a nicer brain.

- **Autonomy** — the system chooses the next action from a goal and
  state. Humans declare gates. They do not stitch phases.
- **Continuity** — standing agents that remember. Strategy, taste, and
  unfinished work survive a restart. A worker hired for one task and
  fired is a factory, not a company.
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
  positional. Spend is a stop, not a warning.

## Direction

The destination is far. Judge a change by whether it shortens the
distance, not by whether it arrives.

Closer means: more of the loop runs without a human starting the next
step; more of the world is reachable under policy; more memory and
coordination survive a restart — and authority is not looser than
before.

A change that only furnishes the cage, or that buys capability by
weakening the charter, is the wrong direction even if it ships.

## The tree is not the vision

This file is the source of truth. The repo is not always caught up —
the project is moving through phases, and leftovers from earlier ones
remain. Old names, old control flow, old assumptions.

When you find something incoherent with this vision, stop and ask.
Do not paper over it, do not reinterpret it into the new world, do
not silently delete it. We will sort it out together.

## Glossary

One name per concept, everywhere: code, comments, docs, tests, CRs.
Check new names against this table; if a concept is missing, add it here
first.

| Term | Meaning | Not |
|---|---|---|
| **role** | a catalog entry: tool ceiling, spawn authority, the procedures it may run — `pm`, `implementer`, `reviewer`, `contract-author` | role template, executor, engineer, sdet, author, tier |
| **crew** | a named list of roles fulfilled together for one run (`feature-delivery`, `feature-review`, `contract-authoring`); `crew_template` names it in policy input and registry rows | — |
| **agent** | one principal: one pod, one ServiceAccount, one registry row; standing (`<standing_name>-<n>`) or ephemeral (`<crew>-<role>-<run_id>`) | brain host, member, instance, participant (the `run_participants` table and its API keep the name; prose says ephemeral agent) |
| **agent host** | the process in the agent image: the dispatch HTTP API plus the SDK loop that runs procedures | brain host, runner |
| **standing name** | the hire's name: the key under `standing:`, the Deployment/Service name | standing role |
| **conductor** | the standing agent a crew is requested under; at runtime `{agent_id, role, url}` | — |
| **procedure** | the agent host's unit of work: `scope`, `decide`, `implement`, `deliver`, `review` | mode, runner, phase, loop, runtime |
| **dispatch** | the orchestrator asking an agent to run a procedure on a text | — |
| **brain** | the model behind the LLM gateway, nothing else (mock brain, paid brain) | — |
| **workflow** | a Temporal workflow = a blueprint section: `intake`, `implement`, `review`, `heartbeat` | family |
| **goal / task / observation** | the text in flight, by origin: intake scopes a goal, implement works a task, the heartbeat decides on an observation | brief |
| **profile** | the deployment's activity-bound sizing (`TIMEOUT_PROFILE`: `default` for a paid brain, `fast` for the mock), learned by every workflow from its first config activity | fast timeouts, timeouts on the input |

## Working rules

- **LLM cost guardrail**: the mock LLM is the default for e2e runs.
  Never set `REAL_LLM=1` or use a paid provider without explicit
  opt-in from the user. The harness now enforces this: the e2e suite
  and `scripts/acceptance.sh` refuse a paid brain (read from the
  deployed `llm-proxy` image, never from `.env`) unless
  `POSEIDON_ALLOW_PAID=1` is set — that variable is the opt-in
  spelling, and setting it still requires the user's say-so.
- **Tests**: run pytest per-package, not from the repo root. The full
  acceptance suite is `scripts/acceptance.sh`; `test_acceptance.py` is its
  index and capstone.
- **Responses API surface**: llm-gateway and mockllm both speak the
  OpenAI Responses API (`gpt-5*` models auto-select it). Any new LLM
  API surface must land in both services.
- **Cluster**: the target environment is kind. Beware that `kind-up`
  can fill the disk, and that `:latest` image tags can leave stale
  pods running old code — check what's actually deployed.
- **"Converged"** for the control plane includes OPA bundle
  activation, not just pods being Ready.
- **Comments**: write them for a reader who does not know the
  implementation plan. Never mention phases, contract IDs (C2, C4,
  …), or how the code used to work. If you find such comments, strip
  them as you touch the file — they are debris, not documentation.
- **Leftovers**: if code, docs, or names contradict the vision above,
  ask immediately. Do not guess which world they belong to.
- **Configuration tiers**: a knob lives in exactly one place. Would a
  human revoke it with a diff? Blueprint. Does it name where something
  runs, which image, which secret, which model? Chart values, spelled
  once. Must two components agree on it? One constant in `common`.
  Nothing is "configurable in principle": a setting no template sets is
  a constant or gone, and a blueprint field nothing reads is gone.
