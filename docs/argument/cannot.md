# What it cannot do

Stated plainly, because a security posture you cannot describe honestly is
not one you can rely on.

## Not hardened: the sandbox is a container, not a VM

There is no gVisor or Kata RuntimeClass. The container's own privilege
boundary is **not** claimed as a security boundary, and a kernel exploit
inside a sandbox is out of scope for what this design defends against.

What *is* claimed, and tested: privilege inside the sandbox reaches nothing.
The sandbox runs as uid 10001 with no privilege-escalation path and all
capabilities dropped — but every containment assertion in the red-team suite
is written to hold at any uid, because the non-root posture is defence in
depth on top of the containment, not the containment itself.

## Unauthenticated in-cluster services, guarded only by NetworkPolicy

Five surfaces have no authentication of their own:

- the **sandbox tool server** (which exposes `/exec` and runs what it is
  given — it *is* the sandbox; that is its job),
- the **agent's `/task` API**,
- the **OPA bundle server**,
- **OPA's own API**,
- the **Temporal frontend** (its browsable web UI is opt-in — the kind
  bootstrap deploys it only with `TEMPORAL_WEB=1`).

All five are protected by being unreachable: NetworkPolicy admits exactly
the callers whose job they are — one for the first four, and for the
frontend the orchestrator worker and [the console](../reference/console.md).
This is a real reduction in defence in depth compared to authenticating
them, and it means **NetworkPolicy enforcement is load-bearing**. On a
cluster whose CNI ignores NetworkPolicy, these are open. The kind
bootstrap installs Calico for exactly this reason.

The frontend keeps one road the fence cannot close: a human with cluster
credentials reaches it by `kubectl port-forward`, which enters the pod's
own network namespace beneath any policy. That reach is not merge
authority — a signal delivered there without a verdict the console
recorded releases nothing, and the acceptance suite proves it on every
run — but it is still reach into the workflow engine, and
[the Temporal contract](../reference/temporal.md) writes down exactly
what it buys.

The one **authenticated** surface a person reaches is the console, and it
is deliberately the only one: on kind a NodePort on the host's loopback,
nowhere an ingress. Exposing it beyond the host is a decision this
prototype has not made, and the token lifetimes, revocation and phishing
questions that come with it are not answered here.

## The merge release trusts the console and the registry

The merge is released on a record, not on a signal: the console writes
the verdict under the verified person's name before it delivers the
signal, the workflow reads that record back, and the compiled
`merge.resolve` decision releases the merge on the record — or refuses by
name. OPA decides on an artifact it can read; a signal alone merges
nothing.

What that buys is bounded by what it trusts, and the trust is named:

- **The console and the registry.** The record is an audit row the
  console wrote; a compromised console, or a writer of `audit_events`,
  could forge one. The row is not signed. The console is trusted code
  like the gateways, and the sixth writer of a table five already share.
- **The applier.** The first apply of a company is decided by nobody —
  no bundle exists yet — and any later apply can rewrite `company.humans`,
  so whoever may apply may name who answers the gates. That is the
  authority a kubeconfig always held; it is now visible and audited under
  a name.
- **The budget gate.** Its verdicts are decided and recorded by the
  console like the merge gate's, but the workflow does not read them
  back: a `budget` signal delivered past the console resumes a run under
  nobody's name. It cannot spend past the stop — the re-dispatch runs
  under the same per-run cap the LLM gateway enforces from the ledger —
  but "spend is a stop, not a warning" is kept by the ledger here, not by
  a verified verdict. A known gap, not a residual.

## Supply chain: tags, not digests

Third-party images are pinned to versioned tags (`openpolicyagent/opa:1.19.0`,
LiteLLM `v1.95.1`, `postgres:16.6`), and Python dependencies are
lockfile-pinned with the images built via `uv export --frozen` — CI asserts
the image resolves the same version the lockfile does.

Base images are **not** digest-pinned, so `python:3.12-slim` can move under
you. Digest pinning is a deliberate policy decision that has not been taken.
CI runs an advisory `pip-audit` on every push.

## Prompt content is inherently untrusted

Workspace filenames, file contents, test output and issue bodies all flow
into agent prompts. That is not a bug to be fixed — it is what makes the
agent useful — and it means **prompt injection is always possible**.

The design response is not to sanitise the text. It is to make a successfully
injected agent unable to do damage: no credentials, a compiled tool scope,
an audited egress chokepoint, and a human gate before anything merges. When
you read the red-team tests, notice that none of them assert the model
refused. They assert the platform refused.

## Deployment maturity

- **kind only.** Never run against a managed cluster. Nothing in the design
  assumes kind, and the acceptance script is cluster-neutral, but "should
  work" is not "has worked."
- **Single replica** for every service; no HA, no leader election.
- **Dev secrets remain.** The chart *requires* every secret value — a
  bare `helm install` fails rather than deploying a placeholder — but the
  kind bootstrap supplies development defaults for the non-credential
  ones.
- **One company per cluster** is what has been exercised, though namespacing
  and per-company bundle roots are designed for more.

## A brain pod that dies mid-dispatch starts its dispatch over

Durability covers the *orchestrator*: kill its worker and Temporal replays
the workflow, re-attaching to a dispatch still in flight. It does not cover
the *brain pod*. An agent pod mounts nothing but its token and is never
restarted, so the model's conversation lives only in that process; if the
pod dies, the retry re-dispatches from scratch against the sandbox as it
stands — the files already written survive, the transcript does not.

Persisting the conversation would need somewhere the brain can write that
is neither the pod nor the sandbox: a gateway write channel, which is new
authority for the one process assumed compromised. It is a decision not
yet taken, not an oversight; the durable state of a run is the sandbox and
the tickets, by design.

## A delegated merge gate merges without a human — with named residual risk

Delegating the merge gate hands a real decision to compiled policy, and
that is authority a human chose to grant in a document they can read and
revoke. What it does *not* remove is honest to state:

- **Code that satisfies the human-approved contract but is wrong in ways
  the contract does not test still merges.** This is the residual the
  [split oracle](anatomy.md#the-split-oracle-who-owns-the-tests) leaves. The platform does not take the
  implementer's own suite as the rubric: the gating fact is the contract
  tree as it stands on `main`, authored by a different crew and merged
  under a human's hand, run against the PR head. So an implementer cannot
  weaken the oracle to launder a merge. What remains is that a
  contract is only as good as the human who approved it — a spec that is
  vacuous, or simply silent about the bug, will pass, and the code merges
  unattended. That is the authority the human exercised by merging the
  contract, and it is why a contract PR always stops at a person and arrives
  with the platform's deletion facts attached. Making the spec *provably*
  adequate (mutation testing, coverage floors) is deliberately out of scope
  — the contract tree is tamper-evident and independently authored, not
  proven sufficient.
- **The reviewed tree and the merged tree can diverge.** The changed paths
  and the verified tree are pinned when the review run starts (its sandbox
  is provisioned from the PR head read then); the merge does not re-assert
  the head SHA. A PR head that moves between review and merge re-opens the
  gap between what was reviewed and what lands — recorded, not yet closed.
- **The rework bound parses a ticket title, but nothing can rewrite it.** The
  rework-chain depth is read back from the platform-written ticket title.
  No company role holds a tool that can edit an issue's title, so a
  compromised conductor cannot walk a depth downward — the mutation path is
  positionally closed. A depth the platform stores on its own row rather
  than parses back would be more robust still, and is flagged in the code.

The compiled decision does refuse what it claims to: a steered reviewer
cannot merge a change touching a protected surface, an implementer cannot
PR the contract tree or launder a red baseline to green, and a steered
contract author's gutted spec parks on a human with its deletions counted —
`test_split_oracle_redteam` drives exactly these on the cluster. The point
of this page is that "policy can merge" is a bounded grant with edges worth
knowing, not a blank cheque.

## The company writes its own documents — with named residual risk

Below the vision, every document the company works from is its own:
the strategy and the architecture masterplan are agent-written, in git,
and read back as untrusted text. What that leaves honest to state:

- **The first strategy is agent-written.** A human chartered the vision;
  the company's reading of it is what every later slice is scoped from.
  Under a human merge gate the strategy's pull request stops at a person;
  under a delegated gate it merges on the reviewer's verdict alone, because
  markdown has no oracle — no `contract_green` for a bet. Whether that
  reading is *right* is not a fact the platform can establish.
- **No strategy learning.** The strategy is written once and then no
  current role may change it — the write-once rule is a compiled deny,
  not restraint. A future standing **observer** would be its author;
  until it exists, a strategy that has been delivered keeps the PM
  scoping, and `idle` is the PM's own judgement. `max_open_tickets` and
  the daily cap bound what that costs; nothing bounds how long it goes on.
- **A poisoned strategy steers scope within policy.** Injection into the
  documents is assumed: the PM files what a poisoned strategy tells it to,
  and the ticket carries the poison to the crew. Containment is the same
  as everywhere — the compiled crew list, the path fences, the egress
  allowlist, the caps — so a poisoned document can pick *which* in-class
  work happens, never work outside the authority. The red team
  (`tests/e2e_kind/test_injected_conductor.py`, `test_documents.py`) drives
  exactly that.
- **A bootstrap ticket nobody can fulfil halts, and stays halted.** When
  every attempt on a bootstrap ticket aborts short of a delivery, the loop
  records a `heartbeat_bootstrap_exhausted` halt and files no more; a
  human closes the ticket and the loop refiles. A bootstrap pull request a
  human closed without merging is the other shape: the ticket concluded
  (it delivered) and the document is still absent, so the loop names no
  halt and idles, scoping refused, until a human closes the ticket and
  the loop refiles. The loop does not guess either way.
- **A retried ticket may ask again.** An implementer that requests an
  architecture change ends its run waiting, by name, and the attempt cap
  decides whether the ticket is offered again. The one-request cap is per
  run: an attempt that runs before the steward's revision merges files a
  second stewardship ticket. Bounded by `max_attempts`, and visible — each
  is titled `Architecture request from run …`.
