# Limitations

Poseidon is a prototype. This page lists what it doesn't do, and where its
guarantees stop. Read it before you trust it with anything that matters.

## Summary

| Limitation | What it means for you | Today |
|---|---|---|
| [The sandbox is a container, not a VM](#the-sandbox-is-a-container-not-a-vm) | A kernel exploit inside a sandbox escapes it. | Out of scope. The sandbox holds nothing worth stealing. |
| [Some services rely on NetworkPolicy alone](#some-services-rely-on-networkpolicy-alone) | On a cluster whose network plugin ignores NetworkPolicy, five internal services are open. | kind-up installs Calico, which enforces it. |
| [The merge record is trusted, not signed](#the-merge-record-is-trusted-not-signed) | A compromised console, or another writer of the audit table, could forge a verdict. | The console is trusted code, like the gateways. |
| [Whoever may apply may name the approvers](#whoever-may-apply-may-name-the-approvers) | Applying a blueprint can rewrite `company.humans`. | Visible and audited under the applier's name. |
| [A budget verdict is not read back](#a-budget-verdict-is-not-read-back) | A budget signal sent around the console resumes a run under nobody's name. | It can't spend past the run's cap. |
| [Base images are pinned by tag, not digest](#base-images-are-pinned-by-tag-not-digest) | `python:3.12-slim` can change under you. | Python dependencies are locked; CI audits them. |
| [Tested on kind only](#tested-on-kind-only) | Nothing has run on a managed cluster or in production. | One replica per service, development defaults for internal secrets. |
| [An agent pod that dies starts over](#an-agent-pod-that-dies-starts-over) | The model's conversation is lost; the files it wrote survive. | The orchestrator itself is durable. |
| [A delegated merge trusts the contract](#a-delegated-merge-trusts-the-contract) | Code that meets the contract but is wrong elsewhere still merges. | Contract changes always need you. |
| [The company writes its own strategy](#the-company-writes-its-own-strategy) | The first strategy is the agents' reading of your vision, and nothing revises it. | Review the strategy pull request yourself. |

## The sandbox is a container, not a VM

There is no gVisor or Kata runtime. The container boundary is not claimed
as a security boundary, and a kernel exploit from inside a sandbox is
outside what Poseidon defends.

What is claimed, and tested, is that privilege inside the sandbox reaches
nothing: no credential, no cluster API, no host mount, and one audited
route out. The sandbox runs as uid 10001 with every capability dropped,
but the red-team tests hold at any uid. Running as non-root is an extra
layer, not the containment.

## Some services rely on NetworkPolicy alone

Five internal services have no authentication of their own:

- the sandbox's tool server, which runs whatever it's given;
- the agent host's `/task` API;
- the OPA bundle server;
- OPA's API;
- the Temporal frontend. Its web UI is deployed only with `TEMPORAL_WEB=1`.

NetworkPolicy admits only the components that need each one. On a cluster
whose network plugin ignores NetworkPolicy, all five are open, which is
why kind-up installs Calico.

A person with cluster credentials can still reach Temporal through
`kubectl port-forward`, which bypasses NetworkPolicy. A merge signal sent
that way releases nothing, because the merge needs a verdict the console
recorded. The acceptance suite checks this on every run. It is still
access to the workflow engine.

The console is the one authenticated service a person reaches. On kind,
it listens on the host's loopback only. Exposing it further, with the
questions of token lifetime, revocation and phishing that come with it,
is not designed yet.

## The merge record is trusted, not signed

A merge is released on the verdict the console recorded in the audit
table, never on the signal alone ([how](merge-gate.md)). That record is
an unsigned row. A compromised console, or another service that writes
the audit table, could forge one. The console is trusted code like the
gateways.

## Whoever may apply may name the approvers

Nobody's policy decides a company's first apply, because no policy exists
yet. Any later apply can rewrite `company.humans`, so anyone allowed to
apply can choose who answers the gates. That authority always came with
cluster access. Poseidon makes it visible and records it under a name.

## A budget verdict is not read back

The console records budget verdicts like merge verdicts, but the workflow
doesn't read them back. A budget signal delivered around the console
resumes a parked run under nobody's name. The run still can't spend past
its cap, because the LLM gateway enforces the cap from the ledger. Issue
[#66](https://github.com/mathbrus/poseidon-prototype/issues/66) tracks
related work on budget verdicts.

## Base images are pinned by tag, not digest

Third-party images are pinned to versioned tags (`openpolicyagent/opa:1.19.0`,
LiteLLM `v1.95.1`, `postgres:16.6`). Python dependencies are locked, and CI
checks that each image installs the locked versions and runs an advisory
`pip-audit`. Base images are not pinned by digest, so `python:3.12-slim`
can change under you.

## Tested on kind only

- Nothing has run on a managed cluster. Nothing in the design depends on
  kind, but that is untested.
- Every service runs one replica, with no high availability.
- The chart requires every secret, but kind-up fills in development
  defaults for the internal ones (database password, egress key).
- A cluster hosts one company.

## An agent pod that dies starts over

If the orchestrator's worker dies, Temporal replays the workflow and the
run continues. An agent pod is different: it keeps the model's
conversation in memory only. If the pod dies mid-step, the retry starts
the step over against the sandbox as it stands. The files already written
survive, and the conversation doesn't.

Keeping the conversation would need somewhere the agent can write that is
neither its pod nor the sandbox. That would be new authority for the one
process assumed compromised, so it hasn't been added.

## A delegated merge trusts the contract

When policy resolves the merge gate:

- **Code that meets the contract but is wrong elsewhere still merges.**
  The implementer can't weaken the contract, because it comes from the
  base branch and another crew writes it. But a contract is only as good
  as the person who approved it. A vague spec passes, and the code merges.
  Proving a spec adequate (mutation testing, coverage floors) is out of
  scope.
- **The reviewed code and the merged code can differ.** The review pins
  the pull request's head when it starts. The merge doesn't re-check it,
  so a push between review and merge lands unreviewed.
- **Rework depth is read from a ticket title.** No agent can edit a
  ticket title, so it can't be lowered. Storing the depth in the registry
  would be sturdier. Issue
  [#32](https://github.com/mathbrus/poseidon-prototype/issues/32) tracks it.

The [merge gate](merge-gate.md) refuses what it claims to refuse.
`test_split_oracle_redteam` drives a manipulated reviewer, implementer and
contract author against it on every acceptance run.

## The company writes its own strategy

- **The first strategy is the agents' reading of your vision.** Under a
  human merge gate, its pull request waits for you. Under a delegated gate
  that includes `docs/`, it merges on the reviewer's verdict alone,
  because a document has no tests.
- **Nothing revises the strategy.** It's written once, and policy denies
  any later change. The PM keeps planning from it until it judges there is
  nothing left to do. `max_open_tickets` and the daily cap bound the cost,
  but not how long that goes on.
- **A poisoned strategy steers work within policy.** The PM files what a
  poisoned strategy says, and the ticket carries it to the crew. The same
  limits apply as everywhere: crew list, protected paths, egress
  allowlist, caps. A poisoned document can pick which allowed work
  happens, never work outside the policy.
- **A bootstrap ticket nobody can fulfill halts the loop.** If every
  attempt fails, the heartbeat records `heartbeat_bootstrap_exhausted` and
  stops planning. If you close a bootstrap pull request without merging,
  the heartbeat idles. In both cases, close the ticket and the heartbeat
  files a new one.
- **A retried ticket may ask for an architecture change again.** One
  request per run is allowed, so a ticket retried before the steward's
  revision merges can file a second request. `max_attempts` bounds it.

## Open questions

These are the places where the design has a real argument to settle.
Discussion is welcome.

1. **Would signing the verdict record help?** It is an unsigned audit row.
   Against which of its writers would a signature protect?
2. **Is run binding the right guard for `git.open_pr`?** Sandbox tools
   require the caller to be bound to the run. Opening a pull request
   doesn't, because it targets the repository. Its guards are the branch
   and path rules instead. Is that the right trade?
3. **How would you detect a buggy compiler?** The platform trusts its own
   compiled policy. The golden files in `packages/compiler/tests/golden/`
   make every change reviewable, but review is not detection.
4. **Should spawning be a ceiling?** The PM can start crews holding tools
   it doesn't have itself. Is that a hole, or the right reading of least
   privilege? [Standing vs. ephemeral agents](../concepts/standing-vs-ephemeral.md)
   argues for the latter.
5. **Which unauthenticated service should get authentication first?** Rank
   the five by the damage a caller could do, now that a Temporal signal
   alone releases no merge.

## Not built yet

Today's roles are the PM, implementer, reviewer, contract author and
steward. A future standing *observer* would read the world outside the
repository and revise the strategy. Support or accounting agents would
enter the same way: as a catalog role with its own tools and procedures,
contained by compiled policy. They need new gateway tools, and runs that
don't assume a sandbox and a repository. None of this exists yet.
