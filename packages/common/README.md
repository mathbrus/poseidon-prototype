# common

The shared library every other package imports: caller identity, workload
authentication, the audit event model, run ids,
logging, and the pod security posture. It owns the vocabulary the rest of
the system agrees on, and deliberately owns no policy of its own — nothing
here decides whether a call is *allowed*, only who is verifiably making it
and how the decision gets recorded. Keeping authorization out is what lets
both gateways and the console share this code without sharing each
other's rules.

It holds no credentials of its own. It reaches the network in exactly two
places, both opt-in: the Kubernetes TokenReview call in `authn` (only when
a reviewer is configured) and the OTLP log exporter in `telemetry` (only
when `OTEL_EXPORTER_OTLP_ENDPOINT` is set).

## Module map

| Module | Owns |
|---|---|
| `identity.py` | `Identity` — who a verified credential says is calling: agent_id, role, company, lifecycle, plus the standing caller's namespace and the ephemeral caller's run binding and crew coordinates. Also `bearer_token()`, the Authorization-header parser. |
| `authn.py` | `Authenticator`: resolves a bearer credential to an `Identity` and audits every rejection — in workload mode (the gateways: a ServiceAccount resolved through the registry) or human mode (the console: a person the deployment declares, `lifecycle: human`). Shared so the three cannot drift. |
| `audit.py` | `AuditEvent` — one model, one registry table, one ingestion contract for every enforcement point — and `args_digest`, the one way arguments are hashed into it. |
| `policy.py` | The OPA naming contract: the Rego slug, a company's package and its decision path — shared by the compiler that emits bundles and the gateways, orchestrator and console that query them. |
| `opa.py` | The OPA transport: one evaluation of a compiled decision against the live bundle, shared by the orchestrator (`crew.request`, `heartbeat.start`, `merge.resolve`) and the console (every human act). A company with no served bundle is a deny, `no_policy_installed`, never an exception. |
| `workflows.py` | Where the workflows run and how they are named — the task queue, the workflow ids, which a human may start and which gates a human may answer — shared by the worker that hosts them and the console that starts, signals and inspects them. |
| `telemetry.py` | `get_logger(name)`: single-line JSON to stdout, `.bind(...)` for run/agent context. Optional OTLP export behind the `otel` extra. |
| `podsecurity.py` | The pod/container security context the three Python pod-spec builders share (uid 10001, no privilege escalation, no capabilities, RuntimeDefault seccomp). |
| `ids.py` | `new_run_id()` — ULID-based, sortable, `run-`-prefixed. |
| `text.py` | `numbered_lines()` — the `cat -n` rendering the sandbox's edit snippets and the agent host's file reads share, so an edit anchors on the picture it gets back. |

## Identity is a verdict, not a claim

`Identity` is a small Pydantic model, but the rule around it carries the
weight: it is produced *exclusively* by `authn`, from a credential the
platform verified. No request header, no JSON body, no environment
variable ever becomes an `Identity`. Downstream code — the policy input
builders in both gateways — can therefore treat every field as a verified
fact rather than a claim, which is what makes "the decision is a function
of who verifiably asked" true (see the deputy discussion in the
[root README](../../README.md), section 2).

The model also records *how* the caller was verified, via `lifecycle`:
`standing` (a rostered agent with a Kubernetes ServiceAccount),
`ephemeral` (an ephemeral agent bound to one run), `platform` (the
orchestrator itself), or `human` (a verified person at the console —
never a workload, never a catalog role). Compiled policy keys on this field, so the
distinction that section 3 of the root README calls structural is carried
by the identity itself, not inferred from naming conventions.

## One credential shape, checked against live state

Every credential is a projected Kubernetes ServiceAccount token. The
`KubeTokenReviewer` posts it to the cluster's TokenReview API with the
platform's audience; the API server checks signature, expiry, audience,
and that the ServiceAccount still exists. Any TokenReview error fails
closed: an unreachable API server must never widen who the gateway
trusts.

A clean answer maps to an identity in one of three ways. The platform
namespace's orchestrator SA becomes the `platform` lifecycle. Any other
name resolves through the registry — `agent_instances` for standing
agents, then `run_participants` by ServiceAccount name for ephemeral
agents (their SA names carry an `ephemeral-` prefix standing ids never use, so
the lookups cannot shadow each other). Either way the row's recorded
namespace **must** match the token's: the same SA name in another
namespace is an impersonation attempt and is refused.

For an ephemeral caller the row *is* the identity — run binding, crew
(`crew_template`), parentage, company, catalog role. A verified token alone is never enough. Why not? Because a
token can only prove who it was issued to — it cannot say anything about
what happened since. If a run is torn down or an agent is revoked
mid-run, every token that identity holds must die *now*, not at expiry.
Making the live row the second half of the credential turns revocation
into a registry write, checked on every call — and being live state, a
stale token cannot smuggle in stale coordinates.

The cache is shaped around exactly that property. Successful TokenReviews
are cached for `authn_cache_ttl_s` (60s default) so the API server sees
one review per caller per minute rather than one per call — but the cache
stores only the *ServiceAccount*, never the identity. The registry
mapping runs on every request, so terminating an ephemeral agent's identity row (or
firing a standing agent) bites on the very next call, cache notwithstanding.

Every rejection of a *presented* token is written to the audit store under
an `authn_*` rule (`authn_namespace_mismatch`, `authn_no_live_participant`,
…). A wrong-namespace token has to be *visible*, not merely 401'd. A
request with no credential at all gets a 401 and no audit event — probes
and scanners are noise, not governance signals; only rejected tokens are.

## What audience scoping buys

Every credential here names its intended consumer. Projected SA tokens are
minted by the kubelet with audience `poseidon-mcp-gateway` (the default
`token_audience` in `AuthnSettings`), and TokenReview is asked
with that same audience — so the one token an agent pod holds is useless
at the Kubernetes API server or anywhere else that checks audiences. Theft
of the agent's only credential yields the right to *ask the gateway*, and
nothing more. Audience scoping is least privilege
applied to the credential itself: it bounds where a stolen token can be
replayed, independently of who stole it. The console's audience is its
own, `poseidon-console`: a human's token fails at both gateways and a
workload's fails at the console, and each refusal is audited
(`authn_not_a_human`, `authn_not_a_workload`).

## One authenticator, two gateways and the console

The MCP gateway and the LLM gateway are the system's two chokepoints for
agents (only the first holds a credential of its own; see the
[llm-gateway README](../llm-gateway/README.md)), and they must answer
"who is calling?" identically — an identity the MCP gateway refuses but the LLM gateway
accepts would be a policy bypass through the cheaper door. Putting the
`Authenticator` (and `AuthnSettings`, which both gateways'
settings classes inherit) in `common` makes drift structurally difficult:
one change moves both, and one env layout configures both.

The console — the chokepoint a person acts through — constructs the same
`Authenticator` in human mode. The TokenReview is the same call with the
console's audience; what a clean answer maps to is a person, not a row:
on a cluster without an identity provider the ServiceAccount
`human-<name>` the chart declares in the platform namespace, elsewhere
the API server's own user. The registry is never consulted for who a
person is. A workload presenting itself at the console is refused
(`authn_not_a_workload`), a person at a gateway (`authn_not_a_human`).

Each chokepoint names itself on its authn-reject events: the
`Authenticator` takes a `source` at construction (`"gateway"` by default;
the LLM gateway passes `"llm-gateway"`, the console `"console"`), so a
reject is attributable to the chokepoint that produced it rather than
defaulting to the MCP gateway.

## Audit events

`AuditEvent` is the one shape every enforcement point writes — the MCP
gateway's decision pipeline, the LLM gateway, the egress proxy
(`sandbox-infra`), the orchestrator's fulfill machinery
(`process-lifecycle`), the process gates (`process-gate`) and the console
(`console` — the one source whose rows carry a person's name). One model,
one registry table, one ingestion contract means one `run_id` query
returns the whole story across sources. `source` is a closed `Literal`
vocabulary so a typo'd producer fails validation loudly instead of
becoming a silently unqueryable source, and the attribution fields
(`company`, `lifecycle`, `parent_agent_id`, `workflow`) are empty strings
only when genuinely absent, never as a shortcut. Args are recorded as
`args_digest`, never values: the audit store must not become a leak
channel for file contents or prompts.

## Logging and telemetry

`get_logger(name)` emits one JSON object per line to stdout, and
`.bind(run_id=..., agent_id=...)` returns a new logger that stamps those
fields on every subsequent line — so run attribution is set once, not
repeated at every call site. Stdout JSON is the always-on contract;
`kubectl logs` keeps working whether or not an exporter exists. When
`OTEL_EXPORTER_OTLP_ENDPOINT` is set *and* the `otel` extra is installed,
an OTLP handler is attached alongside: bound fields become flat, primitive
log-record attributes. Telemetry must never take a service down — a
missing SDK or unreachable collector degrades to stdout-only with one
warning line, never an exception at import time.

## Entry points

A library only — no service, no CLI, no Dockerfile of its own. It is
installed into every image that needs it.

The `otel` extra carries the OpenTelemetry SDK. Platform services depend
on `common[otel]`; the sandbox toolserver image depends on bare `common`,
so the container that runs untrusted workspace code never carries the SDK.
Export stays env-gated at runtime either way.

## Cross-package interactions

- [`gateway`](../mcp-gateway/README.md) and
  [`llm-gateway`](../llm-gateway/README.md) both construct an
  `Authenticator`; their settings classes inherit `AuthnSettings`.
  [`console`](../console/README.md) constructs one in human mode, and
  queries OPA through `opa` and addresses workflows through `workflows`,
  as the orchestrator does.
- [`orchestrator`](../orchestrator/README.md) binds ephemeral
  identities (`orchestrator/activities/crew.py` writes the identity
  row); both gateways resolve them. [`registry`](../registry/README.md)'s
  `RunParticipants.by_service_account` is the live-row half of
  verification.
- `sandbox`, `orchestrator` and `reconciler` build pod specs from
  `podsecurity`. The Helm chart keeps its own copy of the same posture in
  `deploy/helm/poseidon/templates/_helpers.tpl` — templates cannot import
  Python, so those two must be changed together.
- Every producer of audit events (both gateways, the egress proxy, the
  orchestrator's fulfill activities and process gates, the console)
  constructs `AuditEvent`.

## Invariants

- `Identity` is only ever produced by `authn`. Nothing else constructs
  one from untrusted input.
- An ephemeral identity is valid only while its `run_participants` row is
  live; the registry mapping is re-read on every call — the TokenReview
  cache stores the ServiceAccount, never the identity.
- Audit args are digests, never values, and `source` is a closed
  vocabulary.
- `RUN_AS_UID` (10001) in `podsecurity.py` matches the `useradd` line in
  the `Dockerfile` at the repo root. They move together.

## Tests

```
uv run pytest packages/common
```

The suite covers the audit event shape and its closed vocabularies,
logger binding on stdout and through the OTel
handler, and run-id format and uniqueness. `authn` is exercised end to end
by the gateway suite (`packages/mcp-gateway/tests/test_authn.py`), which has
the registry doubles and a fake TokenReview to drive it with.
