# common

The shared library every other package imports: caller identity, workload
authentication, the audit event model, the platform JWT format, run ids,
logging, and the pod security posture. It owns the vocabulary the rest of
the system agrees on, and deliberately owns no policy of its own — nothing
here decides whether a call is *allowed*, only who is verifiably making it
and how the decision gets recorded. Keeping authorization out is what lets
both gateways share this code without sharing each other's rules.

It holds no credentials of its own. It reaches the network in exactly two
places, both opt-in: the Kubernetes TokenReview call in `authn` (only when
a reviewer is configured) and the OTLP log exporter in `telemetry` (only
when `OTEL_EXPORTER_OTLP_ENDPOINT` is set).

## Module map

| Module | Owns |
|---|---|
| `identity.py` | `Identity` — who a verified credential says is calling: agent_id, role, company, lifecycle, plus the standing caller's namespace and the ephemeral caller's run binding and crew coordinates. Also `bearer_token()`, the Authorization-header parser. |
| `authn.py` | `Authenticator`: resolves a bearer credential to an `Identity` and audits every rejection. Shared by both gateways so they cannot drift. |
| `platformjwt.py` | The short-TTL platform JWT: compact JWS, HS256, stdlib only. `mint` / `verify` / `issuer_of`. |
| `audit.py` | `AuditEvent` — one model, one registry table, one ingestion contract for every enforcement point. |
| `telemetry.py` | `get_logger(name)`: single-line JSON to stdout, `.bind(...)` for run/agent context. Optional OTLP export behind the `otel` extra. |
| `podsecurity.py` | The pod/container security context the three Python pod-spec builders share (uid 10001, no privilege escalation, no capabilities, RuntimeDefault seccomp). |
| `ids.py` | `new_run_id()` — ULID-based, sortable, `run-`-prefixed. |

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
`ephemeral` (a crew member minted for one run), or `platform` (the
orchestrator itself). Compiled policy keys on this field, so the
distinction that section 3 of the root README calls structural is carried
by the identity itself, not inferred from naming conventions.

## Two credential shapes, both checked against live state

`Authenticator.authenticate` accepts exactly two credential shapes and
routes between them with `platformjwt.issuer_of` — a deliberately
*unverified* peek at the `iss` claim, used only to pick a verifier, never
to grant trust.

**Platform JWT** (ephemeral crew members). The signature, audience and
expiry checks are necessary but not sufficient. The verifier then requires
a live `run_participants` row in the registry:

```python
# Signature alone is never enough: the live participants row is what
# makes revocation a registry write, effective before TTL expiry.
row = await asyncio.to_thread(self._participants.active, agent_id)
if row is None or row["run_id"] != claims.get("run_id"):
    await self._reject(path, "authn_no_live_participant", agent_id or "unknown")
    return None
```

Why not trust the signature alone? Because a signature can only prove the
token *was* valid when minted — it cannot say anything about what happened
since. A bearer token, once issued, is out of the platform's hands; if a
run is torn down or an agent is revoked mid-run, every token that identity
holds must die *now*, not at TTL expiry. Making the participants row the
second half of the credential turns revocation into a registry write,
checked on every call. The row is also where the crew coordinates
(`crew_template`, `crew_role`) and parentage come from — live state, not
token claims — so a stale token cannot smuggle in stale coordinates.

**Kubernetes service-account token** (standing agents and the
orchestrator). The `KubeTokenReviewer` posts the token to the cluster's
TokenReview API with the platform's audience; the API server checks
signature, expiry, audience, and that the ServiceAccount still exists. A
clean answer maps to an identity in one of two ways: the platform
namespace's orchestrator SA becomes the `platform` lifecycle, and anything
else must resolve through the registry's `agent_instances` row — whose
recorded namespace **must** match the token's. The same SA name in another
namespace is an impersonation attempt and is refused. Any TokenReview
error fails closed: an unreachable API server must never widen who the
gateway trusts.

The two paths cache differently, and the asymmetry is the point.
Successful TokenReviews are cached for `authn_cache_ttl_s` (60s default)
so the API server sees one review per caller per minute rather than one
per call — which means revoking a ServiceAccount can take up to a minute
to bite. The ephemeral path never rides this cache: the participants row
is re-read on every single call, because immediate revocation is the
entire reason that row exists.

Every rejection of a *presented* token is written to the audit store under
an `authn_*` rule (`authn_namespace_mismatch`, `authn_no_live_participant`,
…). A wrong-namespace token has to be *visible*, not merely 401'd. A
request with no credential at all gets a 401 and no audit event — probes
and scanners are noise, not governance signals; only rejected tokens are.

## The platform JWT, and why it is hand-rolled

`platformjwt.py` implements compact JWS with HS256 in ~115 lines of
standard library. Small on purpose: the token format is load-bearing
security code, and the root README's stance is that such code should be
readable in one sitting. The classic JWT vulnerability is baked out rather
than configured away:

```python
# The algorithm is pinned, never read from the header: an attacker
# controls the header, and honoring it is the classic JWT downgrade.
if header.get("alg") != "HS256":
    raise TokenError("bad_token")
if not hmac.compare_digest(signature, _sign(key, signing_input)):
    raise TokenError("bad_signature")
```

The verifier never asks the token how it should be verified — `alg: none`
and RS256-to-HS256 confusion both die at that first check — and the
comparison is constant-time. `mint` writes `iss`/`aud`/`iat`/`exp` *after*
merging caller claims, so a caller cannot override the reserved fields.
`TokenError.reason` is a stable slug (`bad_token`, `bad_signature`,
`wrong_audience`, `expired`) that the authenticator prefixes with `authn_`
to form the audit rule — the failure taxonomy is part of the contract, and
the test suite pins every one of them.

Be honest about the weaknesses. HS256 is symmetric: minter and verifiers
share one key (`platform-jwt-key`), so any service that can *verify* a
token could also *mint* one — a gateway compromise is a minting
compromise. Asymmetric signing would separate those powers. There is no
key-id header and no rotation story, no `nbf`, no clock leeway. And it is
still a hand-rolled crypto-adjacent primitive, which is a category of
thing you should generally not write; the root README lists replacing it
with a maintained library as a sensible future change (section 9). The
mitigations are the alg pin, the tiny surface, and the fact that the
signature is only half the credential anyway. The key never reaches a
company namespace, so nothing an agent can read allows it to mint an
identity.

## What audience scoping buys

Every credential here names its intended consumer. Projected SA tokens are
minted by the kubelet with audience `poseidon-gateway` (the default
`token_audience` in `WorkloadAuthnSettings`), and TokenReview is asked
with that same audience — so the one token an agent pod holds is useless
at the Kubernetes API server or anywhere else that checks audiences. Theft
of the agent's only credential yields the right to *ask the gateway*, and
nothing more. Platform JWTs carry an `aud` claim checked in `verify`;
a token minted for a different audience fails with `wrong_audience` before
its other claims are even considered. Audience scoping is least privilege
applied to the credential itself: it bounds where a stolen token can be
replayed, independently of who stole it.

## One authenticator, two gateways

The MCP gateway and the LLM gateway are the system's two chokepoints
(only the first holds a credential of its own; see the
[gateway-llm README](../gateway-llm/README.md)), and they must answer
"who is calling?" identically — an identity the MCP gateway refuses but the LLM gateway
accepts would be a policy bypass through the cheaper door. Putting the
`Authenticator` (and `WorkloadAuthnSettings`, which both gateways'
settings classes inherit) in `common` makes drift structurally difficult:
one change moves both, and one env layout configures both.

One known wrinkle: `Authenticator._reject` stamps `source="gateway"` on
every authn-reject event, including those the LLM gateway produces — the
LLM gateway's own *decision* events say `source="llm-gateway"`, but its
authn rejects do not distinguish themselves.

## Audit events

`AuditEvent` is the one shape every enforcement point writes — the MCP
gateway's decision pipeline, the LLM gateway, the egress proxy
(`sandbox-infra`), the orchestrator's fulfill machinery
(`process-lifecycle`) and the process gates (`process-gate`). One model,
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

- [`gateway`](../gateway/README.md) and
  [`gateway-llm`](../gateway-llm/README.md) both construct an
  `Authenticator`; their settings classes inherit `WorkloadAuthnSettings`.
- [`orchestrator`](../orchestrator/README.md) is the only minter of
  platform JWTs (`orchestrator/identity.py` wraps `platformjwt.mint`);
  both gateways verify. [`registry`](../registry/README.md)'s
  `RunParticipants.active` is the live-row half of verification.
- `sandbox`, `orchestrator` and `operator` build pod specs from
  `podsecurity`. The Helm chart keeps its own copy of the same posture in
  `deploy/helm/poseidon/templates/_helpers.tpl` — templates cannot import
  Python, so those two must be changed together.
- Every producer of audit events (both gateways, the egress proxy, the
  orchestrator's fulfill activities and process gates) constructs
  `AuditEvent`.

## Invariants

- `Identity` is only ever produced by `authn`. Nothing else constructs
  one from untrusted input.
- An ephemeral identity is valid only while its `run_participants` row is
  live; the row is checked on every call and never cached.
- Audit args are digests, never values, and `source` is a closed
  vocabulary.
- The platform JWT is `alg`-pinned and its reserved claims cannot be
  overridden at mint time. It is small and hand-rolled on purpose, which
  is also why it is worth reading before trusting — see section 9 of the
  [root README](../../README.md).
- `RUN_AS_UID` (10001) in `podsecurity.py` matches the `useradd` line in
  every Dockerfile at the repo root. They move together.

## Tests

```
uv run pytest packages/common
```

Twenty tests: the audit event shape and its closed vocabularies, the JWT
mint/verify round trip and every rejection reason (including a forged
`alg: none` header), logger binding on stdout and through the OTel
handler, and run-id format and uniqueness. `authn` is exercised end to end
by the gateway suite (`packages/gateway/tests/test_authn.py`), which has
the registry doubles and a fake TokenReview to drive it with.
