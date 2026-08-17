# Criticisms

A commissioned roast. Everything below is meant seriously, is grounded in
this repository's own code and documentation, and deliberately goes past
what [README section 9](README.md#9-what-it-cannot-do) already concedes —
self-awareness you have already written down does not count as criticism.
The format is the four questions the roast was asked to answer.

---

## 1. Does it make sense? The demo is a puppet show

The README's proudest sentence is that every claim is exercised by 91
end-to-end tests. Here is what those tests actually exercise: a platform
containing zero language models, being attacked by a mock brain that
follows its attack script with the discipline of a stage actor. The
injection tests pass because `mockllm` was *written to obey the
injection*. The governance tests pass because the mock was written to
attempt exactly the violations the policy anticipates.

This is not worthless — it proves the cage is welded shut against the
attacks you thought of. But the whole premise of the project is that
language models fail in ways you did not think of, and the one adversary
the acceptance suite has never faced is a real model being creatively,
persistently weird. `REAL_LLM=1` exists and CI never runs it; as far as
the evidence goes, this is a containment system validated exclusively
against an inmate who read the script. The cage has been tested. The
animal has not.

And what does the caged company do, at full stretch? One PM files a
ticket, one crew ships a URL shortener, one human approves the merge. The
"company" has a headcount of one standing agent and an org chart whose
growth plan is a commented-out employee (`analyst: count: 0`). Calling
this an agent company is branding; what has been built and proven is a
single, very well-guarded CI pipeline. That can still make sense — but
the README should be read with the substitution "company → pipeline"
applied throughout, and it stings that the substitution loses nothing.

---

## 2. The architecture: a maximum-security prison with one inmate

The inventory, for one merged pull request: a schema validator, a policy
compiler, golden files, an operator, CRDs, OPA plus a bundle server, two
gateways, an LLM proxy, an egress proxy, a nine-table registry, Temporal
with four services of its own, and Calico (the hand-rolled JWT this list
used to end with has since been deleted — see §7). The
enforcement-to-work ratio is comfortably ten to one. Defensible in a
governance prototype — that ratio *is* the research object — but the
following structural complaints survive that defense.

**The verify gate is circular.** The platform's flagship claim is that
delivery is gated on verification the agent cannot influence: the
orchestrator runs the tests *itself*, under its own identity. Look at
what it runs: `gateway.run_tests` executes the workspace's test command
in the very workspace the executor has been freely editing all run
(`sandbox.write_file` has no path restrictions — `path_ownership` guards
the PR diff, not the workspace). And even in the PR, `tests/` is owned by
`backend-engineer, sdet` — the executor *is* a backend-engineer. So the
gate of record proves "the code passes tests the agent was free to
rewrite," and the ephemeral reviewer then re-runs the same agent-authored
tests from the head ref. The independent-verification story reduces to:
the agent grades its own homework, twice, and then a human skims the
result. The base ref is recorded in the sandbox (`.poseidon-base`);
nothing uses it to pin a baseline test suite. This is the single biggest
gap between what the architecture claims and what it does.

**The audit log is a diary, not a ledger.** For a project whose answer to
every threat is "and you find out," the audit store is an ordinary
Postgres table with no hash chain, no tamper evidence, and write
discipline enforced by — the docs now say it honestly — an import-graph
convention, not the database. Every platform service shares one DSN. A
compromised platform component can rewrite history undetected, and the
platform components are exactly the hand-rolled, single-maintainer
services a security reviewer would probe first. "One query tells the
story" is only comforting if nothing with write access wants the story
changed.

**Trust is concentrated where the hand-rolling is thickest.** The
orchestrator holds the egress HMAC key, full platform identity, and
pod-creation rights. The "no LLM in the TCB" claim is true and good — but
the TCB it protects is large, custom, and includes a home-made forward
proxy and a home-made permission system, each of which exists in
maintained, battle-tested form elsewhere. (The home-made JWS
implementation this charge originally led with has since been deleted —
§7 — which answers the sharpest instance but not the pattern.)

**The null hypothesis goes unaddressed.** A sceptic's alternative stack:
GitHub branch protection, required reviews, CODEOWNERS, a repo-scoped
fine-grained PAT, one hardened runner, and an off-the-shelf egress
allowlist proxy. That stack gets you protected branches, human gates,
path ownership, credential confinement, and audit — from a vendor, with
an on-call rotation. What does the bespoke platform add? Genuine answers
exist (per-call revocable workload identity, compiled spawn authority,
run-scoped tools, budget metering), but the README never runs this
comparison, and a reader who runs it themselves will notice that the
bespoke layer re-implements a lot of GitHub one level up, with fewer
nines.

**Assorted charges, briefly.** Budget "enforcement" is per-run only; the
monthly cap is arithmetic in a preview. The escalation channel is `api` —
a gate can escalate into a database column that nobody is paged about,
which is escalation in the sense that a message in a bottle is mail.
Everything is single-replica, so the durability story is "Temporal
survives the worker dying" while every service between the agent and the
world is a SPOF. And concurrency — `max_concurrent: 2`, multiple crews,
parallel runs — is compiled, policy-checked, and essentially unexercised
by the suite that gives the project its confidence.

---

## 3. Real problem, or complexity for nothing?

The problem is real. Prompt injection is unsolved, "the model will be
careful" is not a control, and the industry is currently shipping agents
whose security model is a system prompt with feelings. Containment as
infrastructure — no credentials, chokepoints, positional security — is
the correct instinct, and section 1 of the README argues it better than
most published work. This project is not complexity for nothing.

It is, however, complexity aimed at the second-most-important problem.
Audit what the platform can and cannot stop: exfiltration — stopped;
credential theft — stopped (there is nothing to steal); unauthorized
spawn, out-of-scope tools, rogue merge — stopped. Now the failure mode
that actually dominates coding agents in practice: **confidently wrong
code that passes its own tests.** Every chokepoint waves it through. The
policy engine checks *who may do what*, never *whether the work is any
good*; the verify gate is circular (see above); and the final quality
control is one human, reviewing agent output, exactly as they would
without any of this machinery. An injected agent that quietly plants a
subtle bug — the attack with the highest real-world expected value —
crosses this platform's every layer with a green checkmark and an audit
trail politely recording the crime as compliant.

So the honest framing is narrower than the README's: this is a strong
answer to "how do I stop an agent from doing things it was never allowed
to do," and close to silent on "how do I trust the things it was allowed
to do." The second question is the one that gates real adoption, and no
amount of Rego answers it.

---

## 4. Could this become a real autonomous company?

The bricks that exist are the right bricks. If autonomous organizations
ever exist, they will need exactly what this prototype takes seriously:
identity that can be revoked in one write, authority that is declared,
compiled, and diffable, spend that is attributed, actions that leave a
record someone else can audit. Most agent frameworks have none of this
and will need all of it. Building the courthouse before the economy is a
defensible order of operations.

But be honest about the distance. A company, autonomous or not, is a
memory with revenue. This system has neither. Between here and "real
autonomous company" lie, at minimum: continuity (agents with no memory
across runs cannot hold a strategy, a codebase intuition, or a grudge);
coordination (one conductor spawning one crew is an org chart with one
edge — no negotiation, no shared artifacts between concurrent runs, no
priorities); economics (a $5 cap is cost control; a company needs revenue
attribution, ROI per run, and the authority to decide what is worth
building — the intake gate currently outsources exactly the interesting
decision to a human); external actuation (the world is one git repo and
an issue tracker); quality governance (see section 3 — permissions are
the easy third of governance; incentives and review are the other two);
and the legal interface (an entity that cannot sign, own, or be liable is
not a company, it is a process). None of these are visible on this
architecture's horizon; the blueprint schema has no vocabulary for any
of them.

The steelman: nothing about the current design *blocks* those layers,
and the governance substrate would make them auditable when they arrive.
The roast: "we built the audit layer first" is also what you would say
if the audit layer was the only part you knew how to build. The hard
parts of autonomous companies are not permission systems, and the
project's confidence is currently drawn entirely from the part that was
tractable.

---

## 5. What survives the roast

Credit where due, because a roast of a strawman is worthless:

- **The honesty culture is real.** Section 9, the recorded-not-fixed
  red-team findings, "projection, not enforcement" — this repo documents
  its own weaknesses better than most production systems. This document
  existing is itself part of that pattern.
- **Positional security is the right thesis.** "The containment is
  positional, not behavioural" is the correct response to prompt
  injection and most of the field has not caught up to it.
- **Compile-time authority is genuinely good.** A governance mistake
  failing at compile time with a readable error, and *unreviewed scopes
  being unable to exist*, is a design worth copying.
- **Revocation as a registry row**, checked per call, beats token expiry
  everywhere it is used, and the delivery tool being *absent* from the
  implementation dispatch — enforcement by nonexistence — is the most
  elegant single idea in the codebase.

## 6. If the roast earns any fixes

In descending order of return on effort:

1. **Break the verify-gate circularity.** Pin the test baseline at
   `.poseidon-base`, or run a protected suite the executor cannot write,
   or diff head tests against base tests and surface deletions to the
   reviewer. Today's gate flatters the agent.
2. **Run the red-team suite against a real model**, budget-capped, even
   once, even manually. One afternoon of a frontier model probing the
   sandbox is worth more evidence than another hundred mock scenarios.
3. **Chain the audit log.** A per-event hash chain (or an append-only
   sink outside the shared DSN) turns "we log everything" from a
   courtesy into a property.
4. **Sign the human verdict.** Section 9's "merge approval is workflow
   state" exception is closable: require an OPA-checkable approval
   artifact for `git.merge_pr`, and the one single-owner guarantee in
   the system gets a second lock.
5. **Exercise concurrency.** Two simultaneous runs in the acceptance
   suite would convert `max_concurrent`, per-run sandboxes, and the
   spawn-count injection from compiled hope into tested behaviour.

---

## 7. What I would remove

Criticism that never names a deletion is just commentary. Four were
named, ranked by what the deletion buys. The pattern behind all of them:
each is either a second way of doing something the platform already does
well, or a declared capability with no enforcement behind it — and the
project's strength is precisely that declared and real coincide almost
everywhere else. This section has since been acted on (2026-08); each
entry below carries its verdict.

**The hand-rolled platform JWT — removed by removing its reason to
exist.** The only reason ephemeral crew members carried a custom
credential was the borrow posture: planner and executor shared one pod, a
pod has one ServiceAccount, and two principals in one pod need a second
credential shape. The fix was exactly the proposed one: the planner got
its own pod — it is LLM-only and `needs_sandbox: false`, the cheapest pod
there is — so every principal now maps to one pod, one per-run SA, one
projected token, and the entire custom path collapsed onto the
TokenReview machinery that already existed. `platformjwt.py` is gone, the
JWT half of `authn.py` is gone, the orchestrator's minting is gone, the
`platform-jwt-key` Secret is gone, and section 9's "hand-rolled
crypto-adjacent primitive" admission was deleted rather than mitigated.
The participants-row revocation stayed — it was always orthogonal to
token format and it is the good part; it now backs standing and ephemeral
callers alike, live on every call. As a bonus, the e2e "no token-shaped
env var in any company pod" assertion lost its one exemption.

**The llm-proxy hop — evaluated, and kept.** The charge was that the
split buys key-confinement-by-one-hop while costing an extra service, an
extra image, a NetworkPolicy, and a large pinned LiteLLM dependency. The
charge undercounts what the hop does: the agents speak one OpenAI-shaped
wire format, and the proxy is the translation layer that lets that reach
more than one provider (the project explicitly wants OpenAI *and*
Anthropic). Folding it in would not delete the mass — it would relocate
the translation dependency, the provider key, and external egress into
the small auditable chokepoint, trading one pod for a fatter TCB. The
reference-monitor aspiration is "small enough to inspect"; the hop stays,
and the README now states the rationale where it describes the
chokepoint.

**`per_role_monthly` — removed.** This field contradicted the project's
own thesis. The pitch is "declared means compiled means enforced" — and
here was a blueprint field that was declared, validated, previewed, and
enforced by nothing. In this codebase specifically, unenforced governance
configuration is worse than absent governance configuration, because the
document's credibility *is* the product. It is gone from the schema
(fail-closed: a blueprint still declaring it no longer validates) until a
ledger can enforce it; every budget field you can write today is a hard
stop.

**`temporal-web` in the default bootstrap — removed.** Section 9
documents the Temporal frontend as the weakest unauthenticated surface in
the system — and the kind bootstrap deployed a browsable UI over it by
default, for convenience. It is now opt-in (`TEMPORAL_WEB=1`). The
cheapest risk deletion in the repository, taken.

---

<sub>Commissioned criticism. Read alongside
[what it cannot do](README.md#9-what-it-cannot-do), which admits less
but ships more.</sub>
