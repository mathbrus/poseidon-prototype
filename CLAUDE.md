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

## Working rules

- **LLM cost guardrail**: the mock LLM is the default for e2e runs.
  Never set `REAL_LLM=1` or use a paid provider without explicit
  opt-in from the user.
- **Tests**: run pytest per-package, not from the repo root. The full
  acceptance suite is `scripts/mvp-acceptance.sh` (capstone:
  `test_zz_acceptance.py`).
- **Responses API surface**: gateway-llm and mockllm both speak the
  OpenAI Responses API (`gpt-5*` models auto-select it). Any new LLM
  API surface must land in both services.
- **Cluster**: the target environment is kind. Beware that `kind-up`
  can fill the disk, and that `:latest` image tags can leave stale
  pods running old code — check what's actually deployed.
- **"Converged"** for the control plane includes OPA bundle
  activation, not just pods being Ready.
