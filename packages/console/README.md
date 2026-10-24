# console

The console is the human's chokepoint: the one service a person acts on a company through, and reads it through. The [`poseidon` CLI](../cli/README.md) and the acceptance suite call it over HTTP on port 8090 in the platform namespace.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service | `poseidon-console` | `uvicorn --factory console.app:create_app --port 8090` | `REGISTRY_DSN`; a projected token for the MCP gateway's audience | `audit_events`; `paused_by` and `paused_at` on `companies`; a deleted company's Temporal histories |

The acts, the decision each must win, and the routes are on the [console reference](../../docs/reference/console.md), rendered from `common.audit` and the console interface (`common.console`).

## Module map

| Module | Owns |
|---|---|
| `app.py` | The FastAPI app: the routes of `common.console.ROUTES`, the shared auth middleware and the error answers. |
| `service.py` | `ConsoleService`: every act as decide, record, do, and the reads beside them. The bootstrap rule for a first apply. |
| `views.py` | The one translation layer: registry rows, run records, Temporal's answers and the Company CR mapped onto the console interface's models. Nothing else knows both sides. |
| `temporal.py` | The Temporal client: start, signal, cancel and inspect, addressed by `common.workflows`; `delete_executions` erases every execution on the worker's task queue when the company is deleted. Unreachable Temporal raises `Unavailable`. |
| `gateway.py` | The console's two calls at the MCP gateway, as the `console` identity: `repo.read_file` of the company's two documents at the base ref, and `repo.purge` of its repository for a delete with `--purge-remote`. The company's bundle allows those two and nothing else. |
| `crs.py` | The only cluster touch: submit the Company and AgentInstance CRs (the Company carrying the blueprint's text as applied), read the Company back, delete the Company. |
| `config.py` | `ConsoleSettings`: the shared authn fields, `opa_url`, `temporal_address` and `console_humans`. |
| `fakes.py` | A console over the registry's in-memory fakes, a recording Temporal and a scripted OPA, on a fixed clock. For tests and the golden examples only; nothing running imports it. |

Authentication is `common.authn` in human mode, against the console's own token audience ([common](../common/README.md)).

## How it works

### Every act is decided and recorded before it is done

`ConsoleService._act` asks the company's compiled bundle through `common.opa.evaluate_decision`, with the human's name as `agent_id`. The answer, allow or deny, is written to `audit_events` with `source=console`. A deny raises `PolicyDenied` and nothing else happens.

A verdict, a cancel, a pause, a resume and a delete owe a `rationale` of 1 to 1000 characters, stored on the act's audit row. A missing one fails request validation before policy is asked.

The first apply of a company is the one act policy cannot decide, because no bundle exists yet. When OPA serves no policy for the company, the apply is allowed and recorded as `blueprint_apply_bootstrap`. Reads need a verified human and decide nothing.

The app's exception handlers map `PolicyDenied`, `Invalid` and `NoCompany` to 403, 422 and 404. `Unavailable`, `GatewayUnavailable` and `Unfinished` (a delete that could not finish in time) answer 503. The [status codes](../../docs/reference/console.md#status-codes) are on the reference, and so is [what the console holds](../../docs/reference/console.md#what-the-console-holds) in the cluster.

### A verdict is recorded before it is delivered

`resolve_gate` checks that the gate exists on the run's workflow and that the run is parked on it (`PARKED_STATE[gate]`). Then it decides and records the act. The allow must come under `verdict_rule(gate, approved)`, such as `gate_resolve_merge_approve`, or the console raises instead of signaling. Only then does it signal the workflow, with the boolean alone.

The row is the verdict. The review workflow reads it back (`read_human_verdict`) and `merge.resolve` decides on it. A signal sent to Temporal around the console finds no row and releases nothing. See [The merge gate](../../docs/explanation/merge-gate.md).

A pause follows the same order. It is decided and recorded, then `Companies.pause` writes who holds it, then a `wake` signal makes the loop record the pause at once. A heartbeat that is not running yet reads the pause when the worker starts it.

### The documents and the purge go through the gateway

The console holds no git credential. `GET /documents/{kind}` reads the strategy or the architecture masterplan with `repo.read_file` at the MCP gateway, under the console's own ServiceAccount token for the gateway's audience. `common.authn` maps that ServiceAccount to the `console` lifecycle for the applied company, never the platform's, and the company bundle's `console_read_document` allows that one tool on the company's repository, at its base ref, of its two document paths. The text is clipped as the orchestrator clips it for a crew.

A delete with `purge_remote` calls `repo.purge` the same way, allowed by `console_repo_purge` on the company's repository at its base ref. No role or blueprint can name that tool, and the platform identity does not hold it. Everything else is denied by default, and the gateway audits each call under `console`. Agents' authority is unchanged: the console gains one read and one write.

### A cluster hosts one company

`applied()` reads the one company the reconciler recorded in `companies`. An apply compiles the blueprint against the live `agent_instances` rows. It refuses a blueprint naming another company until that company is deleted. When the chart declares humans (`CONSOLE_HUMANS`), it also refuses a `company.humans` entry the chart did not declare.

`crs.submit` writes the Company CR and one AgentInstance CR per standing agent, and deletes the instances the plan dropped. The [reconciler](../reconciler/README.md) creates every real object from them.

### A delete erases the company

`ConsoleService.delete` runs in order, within `TEARDOWN_TIMEOUT_S` in all:

1. The act is decided and recorded, under `company_delete` or, with `purge_remote`, `company_delete_purge_remote`.
2. The heartbeat is held (the pause, with no act of its own) and woken, and the delete waits until its state is `paused`, so no tick starts a run meanwhile.
3. Each running run is cancelled through `cancel_run`, its own audited act, and the delete waits until its workflow has closed.
4. With `purge_remote`, `repo.purge` resets the repository while the company's bundle still allows it.
5. The Company CR is deleted, and the delete waits for the reconciler's teardown: the tombstone served, the namespace gone, every registry row of the company swept, this act's rows with them.
6. Every execution on the worker's task queue is deleted from Temporal, each run of the heartbeat's continue-as-new chain included: a cluster hosts one company, so every one is the company's. The heartbeat's latest run is also named, since the listing may lag it. Temporal ends whatever still runs, and once the company's row is gone the worker no longer restarts the heartbeat.

A step that cannot finish, a purge the git host refuses included, answers 503. What was done stands: the company is still applied, held, and its runs are ended. Every step is safe to repeat, so a second delete finishes it.

## Invariants

All in `tests/test_console.py` unless noted.

- The health probe is the only open route: `test_the_health_probe_is_the_only_open_route`.
- A rejected token is audited with its principal: `test_a_rejected_token_is_audited_with_its_principal`.
- A denied act is a 403 that does nothing: `test_a_denied_verdict_is_a_403_that_signals_nothing`.
- The verdict row comes first: `test_a_verdict_is_recorded_under_the_humans_name_then_delivered`.
- A verdict answers only a parked gate: `test_a_verdict_answers_only_a_question_that_was_asked`.
- An act without a reason records and signals nothing: `test_an_act_without_a_rationale_is_refused_before_anything_happens`.
- The pause is recorded before the loop hears: `test_pause_is_recorded_before_the_loop_hears_and_resume_mirrors_it`.
- Only the first apply is the bootstrap rule: `test_the_first_apply_is_the_bootstrap_rule`, `test_a_later_apply_is_policys`.
- A delete holds the loop, ends the runs, then tears down and erases: `test_deleting_the_company_is_decided_audited_and_ends_its_runs_first`.
- A delete that cannot finish leaves the company held, and a second finishes it: `test_a_delete_that_cannot_finish_leaves_the_company_held_and_a_second_finishes`, `test_a_refused_purge_fails_the_delete_before_the_company_goes`.
- A purge runs after the runs ended and before the company goes: `test_a_delete_may_purge_the_repository_once_the_runs_have_ended`.
- The delete reaches every execution, each run of a running continue-as-new chain included: `test_temporal.py::test_a_deleted_company_leaves_no_execution`.
- One company per cluster: `test_another_companys_blueprint_is_refused_while_one_is_applied`.
- Reads touch nothing: `test_the_reads_need_a_human_and_touch_nothing`.
- The app serves exactly the interface's route table, each route with its request and response models, and the service builds no response dicts: `test_views.py`.
- Every act's preview answers as the act does, for a human the company names and one it does not: `test_acts.py`.
- The Company CR carries the policies section: `test_crs.py::test_the_cr_carries_the_policies_section`.
- The applied blueprint reads back byte for byte, and a blueprint over the limit is refused: `test_the_applied_blueprint_reads_back_byte_for_byte`, `test_a_blueprint_over_the_limit_is_refused`.

## Testing

```bash
uv run pytest packages/console
uv run python scripts/console_interface.py --check
```

`openapi.json` and `examples/` are the console interface, committed: the OpenAPI snapshot and one golden request and answer per route, rendered from `fakes.py` by `scripts/console_interface.py --write`. CI fails when either drifts from the console, and the CLI's tests replay the examples ([console reference](../../docs/reference/console.md#the-console-interface)).

It needs no cluster, OPA or Postgres (see [Contributing](../../docs/developing/contributing.md)). TokenReview is scripted, the registry is in memory, and Temporal, the policy decisions and the CR calls are recording doubles. `test_temporal.py` alone starts a Temporal dev server, fetched on first use, and skips where it cannot; it takes about 35 seconds. On kind, `test_console_identity.py`, `test_console_redteam.py`, `test_pause.py`, `test_review_gate.py` and `test_delete.py` drive the deployed console.

## Related

- [Console reference](../../docs/reference/console.md), [CLI reference](../../docs/reference/cli.md)
- [Security model](../../docs/explanation/security-model.md)
- [Pause, resume and delete](../../docs/how-to/pause-resume-delete.md)
- [mcp-gateway](../mcp-gateway/README.md), the counterpart for agents
