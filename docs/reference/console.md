# The console

The console is the human's chokepoint: the one service a person acts on
a company through — the counterpart of the MCP gateway for agents. A gateway asks "may this
workload call this tool?"; the console asks "may this person perform this
act?". Both get the answer from the same compiled bundle, write it to the
same audit store, and do nothing without it. The `poseidon` CLI is its
client ([the CLI](cli.md)); so is the acceptance suite, and so would be a
web UI or a chat-ops bot — none of them integrates anywhere else.

## Who a human is

Every request but the health probe carries a token the Kubernetes API
server issued for the console's own audience, `poseidon-console`, verified
through TokenReview by the shared authenticator in human mode
([Workload identity](../concepts/workload-identity.md)). On a cluster
without an identity provider a person is a ServiceAccount the chart
declares — `human-<name>` in the platform namespace, one per entry of the
chart value `console.humans` — and a token for it is minted through the
TokenRequest API with the person's own cluster credentials:

```bash
uv run poseidon login owner --save      # kept for later commands; or export POSEIDON_TOKEN=$(…)
```

On a cluster with an identity provider, the API server's own user is the
person; nothing needs declaring. Where humans are declared, a blueprint
may only name them: `company.humans` naming anyone else is refused at
apply, since that person could never log in to act. The console verifies
humans; it does not make them. Either way the registry is never
consulted for who a person is, and the token is worthless anywhere else:
a human's token fails at both gateways, an agent's fails here, and each
refusal is an audit row naming the principal.

## What a human may do

Whether a verified person may act on the company is the blueprint's
question. `company.humans` names them, and the compiler emits one allow
per act, gate and verdict from that list — so the audit row for an act
names exactly what was granted, and revoking a person is a diff:

```yaml
company:
  humans:
    - owner
```

The acts, rendered from the audit registry (`common.audit`) at build
time — the one list every writer and reader spells them from:

<!-- gen: console-acts -->

A verified person the company did not name is refused by
`human_not_recognized`, audited under their name, and nothing happens.

The first apply of a company is the one act policy cannot decide: no
bundle exists for a company before its Company CR installs one. When OPA
serves no policy for the company, any verified human may apply, and the
audit row says `blueprint_apply_bootstrap`. Every later act — including
every later apply — is policy's. An applier can rewrite `company.humans`;
that is the residual [What it cannot do](../argument/cannot.md) names.

## Pausing the company is an act, and so is stopping it

A company always has a heartbeat. To hold it still without ending it,
pause it: `heartbeat.pause` is decided against the blueprint's humans,
audited under the person's name with the reason, then recorded in the
registry where every tick reads it, then the loop is woken so it says so
at once. A paused tick observes nothing, asks the PM nothing and starts
nothing; it records `tick_paused` with the person's name and waits like
any other. Runs already in flight finish; a human may still start a run
by hand, answer a gate or cancel. The compiled `heartbeat.start` decision
reads the pause again at authorize time, so a pause that lands mid-tick
refuses the start by `heartbeat_start_while_paused`. `heartbeat.resume`
lifts it the same way. A blueprint re-apply leaves a pause in place; only
a resume ends it. The pause is a recorded fact the loop reads, not a
signal it trusts.

```bash
uv run poseidon pause --reason "reading the ledger with nothing moving"
uv run poseidon resume --reason "read it"
```

The one stop is deleting the company — and that is a console act like
any other: `company.delete` is decided against the blueprint's humans and
audited under the person's name, then every running run is cancelled
(each its own audited `run.cancel`, so none dies with its namespace),
then the Company CR is deleted and the reconciler's teardown waited for.
The repository and the ledgers keep the company's name; applying the
same blueprint again brings the company back. `kubectl delete company`
still works, as anything with cluster-admin does, but it asks no policy
and records no name.

```bash
uv run poseidon company delete --reason "wrong vision"
```

## The verdict is recorded before it is delivered

A gate verdict is the act that matters most, and its order is the point.
The console decides `gate.resolve`, appends the audit row under the
person's name — `source: console`, `tool: gate.resolve`, the rule naming
the gate and the verdict, the person's reason as the row's rationale (a
verdict without one is refused, as are a cancel and a delete) — and only
then signals the workflow, with the verdict alone: the row is the record. The review
workflow treats the signal as a nudge to look: it reads the console's
recorded verdict back from the audit store and the compiled
`merge.resolve` decision releases the merge on that record, or refuses by
name. A signal that reached the workflow by any other road finds no
verdict and releases nothing ([The Temporal contract](temporal.md)).

## Routes

JSON in, JSON out, a bearer token on every request but `/healthz`. Acts
are POSTs and answer `403` with the denying rule when policy says no;
reads are GETs. `422` names what could not be acted on (an unknown run, a
blueprint that does not validate or names a company other than the
applied one); `404` says no company is applied yet, for every read and
act that needs one; `503` says Temporal was unreachable. Rendered from the console's own route table at
build time:

<!-- gen: console -->

`since` is a registry timestamp: a client that acts and then reads takes
one from `/now` first, and the runs, spend and ticks it reads back are
its own.

## What it holds, and what it reaches

The registry DSN and its own projected token (for TokenReview) — no
`GIT_TOKEN`, no provider key, no signing key. Its cluster authority is
exactly what an apply needs: create, update and delete on the two CRDs
and a look at the company namespace, which the reconciler deliberately
lacks. Its NetworkPolicy admits OPA, Postgres, Temporal's frontend, DNS
and the API server, and nothing else: no company namespace, no git host,
no model. It writes `audit_events` and the pause columns of `companies`, nothing
else in the registry; the reconciler writes the rest of `companies` from
the CR the console submits.

On kind the console is a NodePort on the host's loopback
(`POSEIDON_CONSOLE_URL=http://127.0.0.1:8090`); there is no ingress, and
reaching it from outside the host is a decision this prototype has not
made.
