# The blueprint

Everything the platform enforces comes from one document. This page reads
the shipped example, `examples/hello-saas.yaml`, section by section; the
YAML is included from that file, and the field tables are rendered from
the JSON Schema in `packages/schema/src/schema/blueprint.py`, both at build
time. Where the delegated profile adds a section, its YAML comes from
`examples/hello-saas-delegated.yaml`.

<!-- gen: blueprint-sections -->

A company without `policies` is a choice you make visibly, not a default
you fall into by omission, and a company that declares no `heartbeat` is
simply one whose runs a human starts. Every object refuses unknown keys, so
a typo'd key is an error, never a silently ignored line.

## `apiVersion`

<!-- gen: blueprint-fields:apiVersion -->

## `company` and `stack` — identity and toolchain

```yaml
--8<-- "examples/hello-saas.yaml:company"

--8<-- "examples/hello-saas.yaml:stack"
```

`company.name` becomes the Kubernetes namespace, the Rego package name, and
the OPA bundle root — which is why two companies can never collide, and why
`platform` is a reserved name. `company.vision` is the one text a human
writes: it reaches every procedure but `deliver` through the static
prompt, so the PM decides and scopes, and every crew implements and
reviews, knowing what the company is for — and nobody in the company can
change it. Applying the blueprint again is the only way it changes.

`stack.repo` is the company's subject: the one repository every run works
on, and the branch (`base_ref`) changes are diffed and delivered against.
It compiles into a `repo_not_declared` deny, so a tool call naming any
other repository is refused at the gateway before it reaches the git host —
and into every agent pod, whose tool calls name it.

`egress_allow` is the **entire** list of hosts any sandbox of this company
may reach. It compiles into both the sandbox constraints and the egress
proxy's per-run allowlist: one list, two consumers, no drift. An empty list
means zero egress, which is a perfectly reasonable choice.

<!-- gen: blueprint-fields:company -->

<!-- gen: blueprint-fields:stack -->

## `standing` — the standing agents

```yaml
--8<-- "examples/hello-saas.yaml:standing"
```

One standing agent: the PM. It may spawn two named crews, at most two at a
time — and that cap is enforced in compiled policy, evaluated against a live
spawn count the gateway injects, not by an honour system.

A hire is one instance — the operator names one Deployment and one Service
per standing name — and `count: 0` is a documented growth path that
produces none. Try hiring an `implementer` at `count: 1` and the compiler
refuses: standing coders do not exist here
([Standing vs. ephemeral agents](../concepts/standing-vs-ephemeral.md)).

<!-- gen: blueprint-fields:standing -->

## `tools` — scopes

```yaml
--8<-- "examples/hello-saas.yaml:tools"
```

A crew is the list of catalog roles fulfilled together for one run. The
catalog defines the crews; a blueprint names the ones it puts to work in a
hire's `may_spawn` and a workflow's `crew`, and the compiler emits one
ephemeral agent spec per role of each ([Roles and crews](roles.md)).
`tools` is the one place a role's scope is narrowed, and it applies to
every agent of that role, standing or ephemeral: the reviewer's list drops
the write tools its catalog entry would allow, and a role not listed keeps
its catalog tools — the PM's catalog entry already holds no `sandbox.*` and
no `git.open_pr`, so the conductor that spawns the implementer cannot do
the implementer's job. Spawn authority is not capability. A scope may
narrow the catalog role's tools and never widen them: a document granting
the reviewer `git.open_pr` fails validation with a message quoting the
catalog's list.

<!-- gen: blueprint-fields:tools -->

## `workflows` — where the human gate is declared

```yaml
--8<-- "examples/hello-saas.yaml:workflows"
```

Three workflows, one per phase of the SDLC in
[Anatomy of a run](../argument/anatomy.md). `intake` is the PM-only scoping
pass — no crew, and it declares no `gates` block at all: a gate is an
object naming its resolver, and a gate nobody declares is a stop nobody
takes, so goals a human submits dispatch the PM without a second stop.
`max_verify_retries` bounds how many times a red verify re-dispatches the
implementer before the run fails.

`gates.merge.resolved_by: human` is what makes the review workflow park and
wait for a person; leaving `gates.merge` out entirely means the review ends
without merging at all — an unattended auto-merge is not a default anyone
gets implicitly. Setting it to `policy` instead, with a `delegate` block
naming the path class and a rework bound, hands the merge to the compiled
decision described in [the merge gate](../argument/anatomy.md#the-merge-gate-human-or-policy).
The delegated profile does this:

```yaml
--8<-- "examples/hello-saas-delegated.yaml:review-delegated"
```

The `delegate` block cannot name a protected surface (`.github/`, the
document's own `deny_paths`, or a declared `contract_tests` tree): the
compiler refuses it, because a merged PR must never be able to rewrite the
rules — or the tests — that govern merging. Gate configuration is read
from the compiled plan at runtime — never hardcoded — so changing the
resolver is a visible, reviewable change to the document, not a code edit
buried somewhere.

<!-- gen: blueprint-fields:workflows -->

## `policies` and `budgets` — the guardrails

```yaml
--8<-- "examples/hello-saas.yaml:policies"

--8<-- "examples/hello-saas.yaml:budgets"
```

These compile into deny rules evaluated against **real arguments**. Because
the gateway computes the changed paths before querying policy, a rule about
`.github/workflows/` is enforceable rather than advisory — an agent cannot
modify the CI that checks it. The path rules also fail closed: without the
changed paths in the input they *cannot* be evaluated, so the call is
denied rather than allowed by default. Fail-safe defaults again: the
dangerous condition is the absent input, and absence denies.

The delegated profile adds the split oracle:

```yaml
--8<-- "examples/hello-saas-delegated.yaml:contract_tests"
```

`contract_tests` declares the acceptance tree a delegated merge is graded
by, and the roles that may deliver into it. Its consequences are
deliberately positional and are described in full in
[the split oracle](../argument/anatomy.md#the-split-oracle-who-owns-the-tests)
— only `contract-author` may `git.open_pr` into the tree (the implementer's
attempt dies at the gateway, named), the tree can never be delegated (a
`delegate.paths` overlap is a compile error, the same refusal `.github/`
gets), and the gating merge fact becomes the tree *as it stands on the base
ref* run against the PR head. It is opt-in: a company that declares no
`contract_tests` compiles no such rules and merges exactly as before.
Removing the block reverts to one implementer-owned tree — the split is
authority, so revoking it is a diff.

Every gate declares how long it waits for a human: `deadline_minutes` on
the gate, `pause_minutes` on the budget. When a gate has waited half its
deadline with no verdict, an escalation event fires; at the full deadline
the gate expires. Both transitions are first-class audit events, so
"nobody was home" is a recorded outcome rather than a silent hang — and
the patience is the charter's, not a setting someone chose when they
started the run.

`per_run.usd` is a hard stop at the LLM gateway, not a warning. Exhausting
it produces a distinguishable error, an audit event, an escalation, and an
API-visible paused state that waits `pause_minutes` for the human who may
raise the cap — never silent degradation to a cheaper model. It is also the
*only* budget cap the schema accepts, and that is deliberate rather than a
gap: in a document whose credibility rests on "declared means compiled
means enforced," an unenforced field is worse than an absent one, so no cap
appears here until a ledger enforces it.

<!-- gen: blueprint-fields:policies -->

<!-- gen: blueprint-fields:budgets -->

## `heartbeat` — the company's own initiative (opt-in)

```yaml
--8<-- "examples/hello-saas-delegated.yaml:heartbeat"
```

The one section the shipped example does *not* carry, because absence is a
meaningful state: with no `heartbeat` section, runs start only when a human
starts one, which is everything this reference described so far.

Declaring it grants the platform a bounded authority to start work on its
own initiative: whenever a run of the company finishes — and at least every
`reconcile_minutes` — the named conductor is dispatched to choose the next
action from the backlog, and the chosen workflow may
start only within `max_concurrent_runs`, under the `daily_cap`, and on an
item fewer than `max_attempts` runs have taken up. Every field is required
once the section exists — a defaulted cap is authority nobody wrote down
(an applied section without `max_attempts` refuses every tick as
`tick_refused:max_attempts_undeclared` until a complete one is applied) —
and `intake` is deliberately not startable: goals enter through a human.

`max_attempts` is what bounds an aborted run. An issue or a delivered PR
is off the backlog while a run on it is live, once a run *concluded* it
(an implement run that delivered a PR, a review run whose reviewer reached
a verdict — a rework filed or a gate rejected on that verdict is still a
conclusion), or once `max_attempts` runs took it up. A run that ended
short of any conclusion — a timed-out model call, a step cap, a cancel —
leaves the item for another try, as many as the charter allows; `1` is
the one-shot company where every run is the only try. The run that spends
the last attempt records it as a `gate.heartbeat_attempts` /
`gate_exhausted` process-gate event under its own run id and leaves a
platform comment on the PR or issue, so the abandonment is never silent.

The caps compile into the company's policy bundle as a `heartbeat.start`
decision (deny over-cap, deny absent counts — fail-safe defaults again;
the target item's prior attempts ride the input like the other counts),
which is the decision the heartbeat must win before starting anything.
Only the platform lifecycle can match its allow rules: an agent reciting
`heartbeat.start` at the gateway matches nothing, and a blueprint diff that
removes the section removes the rules, so revocation is the same reviewable
edit as every other authority change.

The loop itself is one durable workflow per company (`heartbeat-{company}`),
kept running by the orchestrator worker while the applied blueprint
declares the section. Ticks are event-triggered: the platform nudges the
loop with a `wake` signal when one of the company's own runs reaches its
end, and `reconcile_minutes` is the backstop — the longest a change only
the outside world saw (an issue filed by hand, a PR merged by hand) can
wait for a look. A wake carries no instruction: the tick it causes
observes, decides and is authorized exactly like one the timer caused.
Each tick observes the open backlog, the delivered
PRs awaiting review (the registry names them, the git host confirms each
is still open — a PR merged or closed by anyone else is not a candidate),
the in-flight runs and today's ledger spend, then
dispatches the standing PM's toolless *decide* procedure: one action —
`implement #n`, `review #n`, or `idle` — with a required one-line
rationale, journaled as a `heartbeat.decide` audit event (the append-only
record of *why* the company did what it did; the PM's cross-run state is
the registry, read fresh each tick — no private memory). The PM proposes
and the platform disposes: the action set is closed, the target must come
from the observation, and the start must still win the compiled
`heartbeat.start` decision — a poisoned ticket title can at most steer the
choice between observed candidates, never mint a new one. Idle ticks and
cap halts record their reason too (a daily-cap halt is a first-class
process-gate event, like the per-run budget stop). Removal is fail-closed
twice over: the tick re-reads the section and stops when it is gone, and a
tick that somehow survived would still find no `heartbeat.start` rule to
match. Issue bodies never feed the tick — only numbers, titles and URLs —
because the backlog is untrusted input, and the implement run resolves the
ticket itself under the platform identity like any other issue-driven run.

<!-- gen: blueprint-fields:heartbeat -->

## Apply it

```bash
uv run poseidon blueprint preview examples/hello-saas.yaml   # what would change
uv run poseidon blueprint apply examples/hello-saas.yaml --yes
```

`preview` diffs the compiled plan against live registry state, so hiring and
firing are visible before they happen. Firing is just a deletion in the
diff — and deleting an agent's ServiceAccount is what revokes its identity,
since the API server then refuses TokenReview for its tokens. The commands
and their flags are on [the CLI reference](cli.md).
