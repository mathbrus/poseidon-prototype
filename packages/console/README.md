# console

The console is the human's chokepoint: the one service a person acts on a company through, and reads it through. The [`poseidon` CLI](../cli/README.md) and the acceptance suite call it over HTTP on port 8090 in the platform namespace.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-console` | `uvicorn --factory console.app:create_app --port 8090` | `REGISTRY_DSN` | `audit_events`; `paused_by` and `paused_at` on `companies` |

The acts, the decision each must win, and the routes are on the [console reference](../../docs/reference/console.md), rendered from `common.audit` and `app.ROUTES`.

## Module map

| Module | Owns |
|---|---|
| `app.py` | The FastAPI app, the route table (`ROUTES`), the request models, the shared auth middleware and the error answers. |
| `service.py` | `ConsoleService`: every act as decide, record, do, and the reads beside them. The bootstrap rule for a first apply. |
| `temporal.py` | The Temporal client: start, signal, cancel and inspect, addressed by `common.workflows`. Unreachable Temporal raises `Unavailable`. |
| `crs.py` | The only cluster touch: submit the Company and AgentInstance CRs, read the Company's phase, delete the Company. |
| `config.py` | `ConsoleSettings`: the shared authn fields, `opa_url`, `temporal_address` and `console_humans`. |

Authentication is `common.authn` in human mode, against the console's own token audience ([common](../common/README.md)).

## How it works

### Every act is decided and recorded before it is done

`ConsoleService._act` asks the company's compiled bundle through `common.opa.evaluate_decision`, with the human's name as `agent_id`. The answer, allow or deny, is written to `audit_events` with `source=console`. A deny raises `PolicyDenied` and nothing else happens.

A verdict, a cancel, a pause, a resume and a delete owe a `reason` of 1 to 1000 characters, stored as the row's rationale. A missing reason fails request validation before policy is asked.

The first apply of a company is the one act policy cannot decide, because no bundle exists yet. When OPA serves no policy for the company, the apply is allowed and recorded as `blueprint_apply_bootstrap`. Reads need a verified human and decide nothing.

The app's exception handlers map `PolicyDenied`, `Invalid`, `NoCompany` and `Unavailable` to 403, 422, 404 and 503. The [status codes](../../docs/reference/console.md#status-codes) are on the reference, and so is [what the console holds](../../docs/reference/console.md#what-the-console-holds) in the cluster.

### A verdict is recorded before it is delivered

`resolve_gate` checks that the gate exists on the run's workflow and that the run is parked on it (`PARKED_STATE[gate]`). Then it decides and records the act. The allow must come under `verdict_rule(gate, approved)`, such as `gate_resolve_merge_approve`, or the console raises instead of signaling. Only then does it signal the workflow, with the boolean alone.

The row is the verdict. The review workflow reads it back (`read_human_verdict`) and `merge.resolve` decides on it. A signal sent to Temporal around the console finds no row and releases nothing. See [The merge gate](../../docs/explanation/merge-gate.md).

A pause follows the same order. It is decided and recorded, then `Companies.pause` writes who holds it, then a `wake` signal makes the loop record the pause at once. A heartbeat that is not running yet reads the pause when the worker starts it.

### A cluster hosts one company

`applied()` reads the one company the reconciler recorded in `companies`. An apply compiles the blueprint against the live `agent_instances` rows. It refuses a blueprint naming another company until that company is deleted. When the chart declares humans (`CONSOLE_HUMANS`), it also refuses a `company.humans` entry the chart did not declare.

`crs.submit` writes the Company CR and one AgentInstance CR per standing agent, and deletes the instances the plan dropped. The [reconciler](../reconciler/README.md) creates every real object from them. A delete is decided first, then cancels each running run through `cancel_run` (each its own audited act), then deletes the Company CR. It waits for the teardown unless the caller passed `wait: false`.

## Invariants

All in `tests/test_console.py` unless noted.

- The health probe is the only open route: `test_the_health_probe_is_the_only_open_route`.
- A rejected token is audited with its principal: `test_a_rejected_token_is_audited_with_its_principal`.
- A denied act is a 403 that does nothing: `test_a_denied_verdict_is_a_403_that_signals_nothing`.
- The verdict row comes first: `test_a_verdict_is_recorded_under_the_humans_name_then_delivered`.
- A verdict answers only a parked gate: `test_a_verdict_answers_only_a_question_that_was_asked`.
- An act without a reason records and signals nothing: `test_an_act_without_a_reason_is_refused_before_anything_happens`.
- The pause is recorded before the loop hears: `test_pause_is_recorded_before_the_loop_hears_and_resume_mirrors_it`.
- Only the first apply is the bootstrap rule: `test_the_first_apply_is_the_bootstrap_rule`, `test_a_later_apply_is_policys`.
- A delete ends the runs first: `test_deleting_the_company_is_decided_audited_and_ends_its_runs_first`.
- One company per cluster: `test_another_companys_blueprint_is_refused_while_one_is_applied`.
- Reads touch nothing: `test_the_reads_need_a_human_and_touch_nothing`.
- The route table matches the app: `test_the_route_table_matches_the_app`.
- The Company CR carries the policies section: `test_crs.py::test_the_cr_carries_the_policies_section`.

## Testing

```bash
uv run pytest packages/console
```

It needs no cluster, Temporal, OPA or Postgres (see [Contributing](../../docs/developing/contributing.md)). TokenReview is scripted, the registry is in memory, and Temporal, the policy decisions and the CR calls are recording doubles. On kind, `test_console_identity.py`, `test_console_redteam.py`, `test_pause.py` and `test_review_gate.py` drive the deployed console.

## Related

- [Console reference](../../docs/reference/console.md), [CLI reference](../../docs/reference/cli.md)
- [Security model](../../docs/explanation/security-model.md)
- [Pause, resume and delete](../../docs/how-to/pause-resume-delete.md)
- [mcp-gateway](../mcp-gateway/README.md), the counterpart for agents
