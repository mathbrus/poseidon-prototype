# common

`common` is the shared library that holds the names and shapes two or more components must agree on: caller identity, workload and human authentication, the audit event model, the OPA naming contract, workflow names and the platform's topology. Every package except `blueprint` and `mock-brain` imports it, and it runs inside their processes.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| library | — (installed into its users' images) | `import common.<module>` | nothing | nothing (callers pass in the audit sink) |

## Module map

| Module | Owns |
|---|---|
| `console/` | The console interface: the request and response models of every console route (`models.py`), the closed sets they use, built from the constants below (`enums.py`), and the route table (`routes.py`). Imports pydantic, the standard library and `common`, nothing else; the console serves it, the CLI and the acceptance suite consume it. |
| `identity.py` | `Identity`, the verified caller, the lifecycle names (the `console` one included), and `bearer_token()`. |
| `authn.py` | `Authenticator`, `AuthMiddleware`, `AuthnSettings`, `KubeTokenReviewer`: a bearer token in, an `Identity` out, every rejected token audited. |
| `audit.py` | `AuditEvent`, the closed `Source` and `Decision` lists, `PLATFORM_EVENTS` (every event name the platform writes) and the console's acts among them, `RATIONALE_LIMIT`, and `args_digest()`. |
| `policy.py` | The OPA naming contract: package and decision paths, `rego_slug()`, the decision names, and `RULES`, every rule name a decision can answer with. |
| `opa.py` | `evaluate_decision()`: one decision against the company bundle. No served bundle answers `no_policy_installed`, a deny. |
| `workflows.py` | Task queue, workflow ids, gates, every state a workflow's `state()` answers (`RUN_STATES`, `HEARTBEAT_STATES`), run statuses, gate outcomes and tick outcomes, with the rule spellings for each. |
| `topology.py` | Ports, Service names, namespaces, token audiences, pod labels, service URLs, the Company phases and the blueprint's size limit. |
| `podsecurity.py` | The pod and container security context: uid 10001, non-root, no privilege escalation, all capabilities dropped, RuntimeDefault seccomp. |
| `agentpod.py` | Resources and readiness probe shared by standing and ephemeral agent pods. |
| `documents.py` | `DOCUMENT_NAMES`: the company's two documents by name, which the blueprint places and the console interface names. |
| `tasks.py` | The task texts of heartbeat-started runs, the regexes that parse them, and the `Crew:` ticket trailer. |
| `repos.py` | `repo_slug()`: a repository URL's `owner/name`, which the gateway addresses GitHub by and the CLI asks for before a purge. |
| `verify.py` | `suite_green()`: what a test run's exit code means. |
| `text.py` | `numbered_lines()` and the clip helpers for untrusted text. |
| `telemetry.py` | `get_logger(name)`: one JSON object per line on stdout, with optional OTLP export. |
| `ids.py` | `new_run_id()`: a `run-`-prefixed ULID. |
| `version.py` | `running_version()`: the `POSEIDON_VERSION` stamp, or `unreleased`. |

## How it works

### Identity comes only from a verified token

`Authenticator` posts each bearer token to the Kubernetes TokenReview API with its own audience. No header, body field or environment variable becomes an `Identity`. Any TokenReview error rejects the call.

In workload mode (the two gateways), a verified ServiceAccount maps in one of three ways:

- The orchestrator's ServiceAccount in the platform namespace becomes the `platform` lifecycle.
- A name with an `agent_instances` row becomes a standing agent.
- A name with a live `run_participants` row becomes an ephemeral agent, carrying its run, crew and parent.

The row's namespace must equal the token's, or the call is refused. In human mode (the console), the principal must be a `human-<name>` ServiceAccount the chart declares, or an identity provider's user. The registry is never consulted for a human.

Successful TokenReviews are cached for 60 seconds, keyed by token digest. The cache holds the principal, never the identity. The registry lookup runs on every call, so deleting a row revokes the identity on the next call. See [workload identity](../../docs/concepts/workload-identity.md).

### Audiences separate the doors

Agent tokens carry the audience `poseidon-mcp-gateway`. Human tokens carry `poseidon-console`. A human's token fails at both gateways (`authn_not_a_human`), and a workload's token fails at the console (`authn_not_a_workload`). Each `Authenticator` takes a `source`, so every reject names the door that refused it. A request with no credential gets a 401 and no audit event.

### One spelling per shared name

Names that two components must agree on live here once, from audit event names to ports. `AuditEvent` stores an argument digest, never the arguments. The one cleartext field is `rationale`, capped at 1000 characters. The Helm chart repeats some values as literals because templates cannot import Python. CI checks that they agree.

Logging writes JSON to stdout, and also exports over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and the `otel` extra is installed. The sandbox tool server depends on bare `common`, so the image that runs workspace code has no OTel SDK.

## Invariants

- The console refuses a workload, and a gateway refuses a human. Both refusals are audited: `tests/test_authn.py::test_a_workload_is_refused_at_the_console`, `::test_a_human_is_refused_at_the_gateway`.
- Workload mode cannot run without the registry readers: `tests/test_authn.py::test_workload_mode_needs_the_registry_readers`.
- A TokenReview that cannot answer refuses the caller, audited: `tests/test_authn.py::test_an_unreachable_token_review_refuses_the_caller`.
- A deleted registry row revokes the identity before the cache expires: `packages/mcp-gateway/tests/test_authn.py::test_row_termination_revokes_before_ttl`.
- Platform event names are unique and use known sources: `tests/test_audit.py::test_platform_event_names_are_spelled_once`.
- A rationale longer than the limit fails validation: `tests/test_audit.py::test_a_rationale_is_bounded`.
- A company with no served bundle is denied, not an error: `tests/test_opa.py::test_evaluate_fails_closed_when_no_policy_is_served`.
- Only `approved` and `automated` release a gate: `tests/test_workflows.py::test_only_a_human_verdict_or_automation_releases`.
- An empty suite is green only when the run is unscoped: `tests/test_verify.py::test_an_empty_suite_is_green_only_unscoped`.
- The chart's ports and token audience equal `topology.py`: the "chart's ports and token audience" step in `.github/workflows/ci.yml`.
- Every image runs as uid 10001, the `RUN_AS_UID` in `podsecurity.py` and the `useradd` line in the root `Dockerfile`: the "Images run as a non-root user" step in CI.

## Testing

```bash
uv run pytest packages/common
```

It needs no cluster, database or network. Run tests per package, as [Contributing](../../docs/developing/contributing.md) explains. The registry-backed authentication cases live in the mcp-gateway suite. On kind, `tests/e2e_kind/test_workload_identity.py`, `test_ephemeral_identity.py` and `test_console_identity.py` exercise the real TokenReview path.

## Related

- [Workload identity](../../docs/concepts/workload-identity.md) and [the security model](../../docs/explanation/security-model.md)
- [OPA and Rego](../../docs/concepts/opa.md) and [the policy reference](../../docs/reference/policy.md)
- [mcp-gateway](../mcp-gateway/README.md), [llm-gateway](../llm-gateway/README.md), [console](../console/README.md): the three `Authenticator` users
- [registry](../registry/README.md): the rows the workload mapping reads
