# common

The shared library every other package imports: caller identity, workload
authentication, the audit event model, the platform JWT format, run ids,
logging, and the pod security posture. It owns the vocabulary the rest of
the system agrees on, and deliberately owns no policy of its own — nothing
here decides whether a call is allowed, only who is making it and how the
decision gets recorded.

It holds no credentials and reaches no network except through the
Kubernetes TokenReview call in `authn`.

## Module map

| Module | Owns |
|---|---|
| `identity.py` | `Identity` — who a verified credential says is calling: company, agent_id, lifecycle, role, plus the standing caller's namespace and the ephemeral caller's run binding and crew coordinates. Produced only by `authn`. |
| `authn.py` | `Authenticator`: resolves a bearer credential to an `Identity`, and audits every rejection. Shared by both gateways so they cannot drift. |
| `platformjwt.py` | The short-TTL platform JWT: compact JWS, HS256, stdlib only. `mint` / `verify` / `issuer_of`. |
| `audit.py` | `AuditEvent` — one model, one table, one ingestion contract for every enforcement point. |
| `telemetry.py` | `get_logger(name)`: single-line JSON to stdout, with `.bind(...)` for run/agent context. Optional OTLP export behind the `otel` extra. |
| `podsecurity.py` | The pod/container security context the three Python pod-spec builders share (uid 10001, no privilege escalation, no capabilities, RuntimeDefault seccomp). |
| `ids.py` | `new_run_id()` — ULID-based, sortable, `run-`-prefixed. |

## Two credential shapes

`Authenticator.identify` accepts exactly two, and both are verified against
live registry state rather than trusted on their signature alone:

- **Platform JWT** — HS256 signature, audience and expiry, *and* a live
  `run_participants` row. The row is what makes revocation effective before
  the TTL runs out: terminating it kills every token of that identity
  immediately.
- **Kubernetes service-account token** — TokenReview with the platform's
  audience. The platform namespace's orchestrator SA maps to the platform
  lifecycle; anything else resolves through the registry's `agent_instances`
  row, whose recorded namespace **must** match the token's. The same SA name
  in another namespace is an impersonation attempt and is refused.

Every rejection of a presented token is written to the audit store under an
`authn_*` rule. A wrong-namespace token has to be *visible*, not merely 401'd.

## Entry points

A library only — no service, no CLI, no Dockerfile of its own. It is
installed into every image that needs it.

The `otel` extra carries the OpenTelemetry SDK. Platform services depend on
`common[otel]`; the sandbox toolserver image depends on bare `common`, so
the container that runs untrusted workspace code never carries the SDK.
Export stays env-gated at runtime by `OTEL_EXPORTER_OTLP_ENDPOINT` either
way.

## Cross-package interactions

- `gateway` and `gateway-llm` both construct an `Authenticator` — the point
  of putting it here is that one change moves both.
- `orchestrator` mints platform JWTs with `platformjwt`; both gateways
  verify them. `registry.RunParticipants` is the live-row half.
- `sandbox`, `orchestrator` and `operator` build pod specs from
  `podsecurity`. The Helm chart keeps its own copy of the same posture in
  `_helpers.tpl` — templates cannot import Python, so those two must be
  changed together.
- Every producer of audit events (both gateways, the egress proxy, the
  orchestrator's process gates) constructs `AuditEvent`.

## Tests

```
uv run pytest packages/common
```

Covers the audit event shape, the JWT mint/verify round trip and every
rejection reason, logger binding, and id generation. `authn` is exercised
end to end by the gateway suite, which has the registry doubles and a fake
TokenReview to drive it with.

## Invariants

- `Identity` is only ever produced by `authn`. Nothing else constructs one
  from untrusted input.
- Audit args are digests, never values: the audit store must not become a
  leak channel.
- The platform JWT is `alg`-pinned. It is small and hand-rolled on purpose,
  which is also why it is worth reading before trusting — see the
  limitations section of the root README.
- `RUN_AS_UID` in `podsecurity.py` matches the `useradd` line in every
  Dockerfile at the repo root. They move together.
