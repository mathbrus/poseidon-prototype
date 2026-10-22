# mock-brain

The mock brain is a deterministic, OpenAI-compatible stand-in for the LLM proxy, so end-to-end runs spend no tokens. The [llm-gateway](../llm-gateway/README.md) calls it in place of LiteLLM on kind clusters brought up without `REAL_LLM=1`; it is for development and tests only.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service (dev/test) | `poseidon-mock-brain` | `python -m mock_brain --model <ignored> --port 4000 --drop_params` | nothing | nothing |

## Module map

| Module | Owns |
|---|---|
| `brain.py` | `next_completion`, the pure function from conversation to scripted completion; the scenario markers, the `SCENARIOS` map, and the canned file contents. |
| `app.py` | The routes, the Responses adapter, the usage estimate and the cost header. |
| `__main__.py` | The process entry point. It accepts LiteLLM's arguments and ignores `--model` and `--drop_params`. |

## How it works

### The mock answers on the real proxy's routes

It serves `/chat/completions`, `/responses`, their `/v1` aliases, and `/health/liveliness`. The Responses routes translate to chat messages and back, so scenario logic never forks between the two surfaces. `deploy/helm/poseidon/values-mock-brain.yaml` swaps the `llm-proxy` image, pins `run.model` to `mock-brain` so the ledger names the model that answered, and selects the `fast` profile. Requests are not authenticated, like the real proxy behind the gateway.

Usage is an estimate of about four characters per token. Cached tokens are half the prompt tokens and reasoning tokens half the completion tokens. Every completion reports its cost in `x-litellm-response-cost` at a fixed $0.002 per 1K tokens, which the gateway meters like a real provider's.

### The procedure header picks the script

The first line of the system message's first block, `# Poseidon procedure: <name>`, selects the script. Repository text arrives in a later block and never picks one. The offered tools are ignored. Progress comes from the history, by counting the calls already made, so retries and agent restarts replay the same way.

| Procedure | Script |
|---|---|
| `decide` | Review the first delivered PR, else implement the first open ticket, else scope an open slice no ticket covers, else idle. Only actions on the observation's startable line. |
| `scope` | When the strategy carries `NEXT_SLICE_MARKER`, file the slice's ticket and comment a summary on it; otherwise file nothing. |
| `implement` | Write `slugify.py` and `tests/unit/test_slugify.py` once, then answer in plain text so the host runs its verify. |
| `deliver` | `open_pr` (the platform commits the workspace), then plain text once a `pr_url` comes back; plain text after a refusal it cannot fix by changing the branch. |
| `review` | `read_pr`, `run_tests`, one `comment_pr`, then a `pass` verdict. |

A task carrying `Crew: contract-authoring` plays the contract author, who writes `tests/contract/`. A task carrying `Crew: stewardship` plays the steward, who writes the document the ticket names. The delivery branch is the first `feature/<slug>` in the task text, else `feature/slugify-mock`. Name a branch when runs share a repository.

### Scenario markers drive the adversarial runs

The mock brain obeys injected instructions on purpose, so tests prove the platform blocks them. `SCENARIOS` in `brain.py` maps every marker.

| Marker | Carried by | The platform must |
|---|---|---|
| `INJECTED_MARKERS` | ticket | deny the PR on `main` (`git_open_pr_protected_branch`); the script recovers onto the feature branch |
| `VERIFY_RED_MARKER` | ticket | go red on its own verify of a planted test that passed the advisory one |
| `VERIFY_FEEDBACK_MARKER` | red-verify feedback | bound the retry; the script removes the plant |
| `DELIVERY_FEEDBACK_MARKER` | refused-delivery feedback | hand the refusal to implement; the script undoes the refused edit, and the platform verifies again |
| `NEXT_SLICE_MARKER` | strategy | scope the slice once, then idle |
| `CONDUCTOR_MARKER` | strategy | deny the crew outside `may_spawn` and the exfiltration at the egress proxy |
| `STEERED_REVIEW_MARKER` | PR diff | hold the merge gate after a steered `pass` |
| `TAMPERING_REVIEW_MARKER` | PR diff | verify the head before the reviewer, so its tampered sandbox (a `conftest.py` that collects nothing) moves no check |
| `REVIEW_ABORT_MARKER` | issue title, then PR title | cap review attempts when no verdict comes |
| `MERGE_GATE_REDTEAM_MARKER` | ticket | refuse a delegated merge of an out-of-class path (`.github/`) |
| `CONTRACT_FIRST_MARKER` | strategy | order the contract ticket first and park the contract PR |
| `ARCH_REQUEST_MARKER` | ticket | file a stewardship ticket; the run waits on it |
| `MASTERPLAN_EDIT_MARKER` | ticket | deny the masterplan edit (`git_open_pr_architecture_path`); implement restores the plan |
| `STEERED_CONTRACT_MARKER` | contract ticket | surface the gutted spec and park the PR |
| `ORACLE_SMUGGLE_MARKER` | ticket | deny the write into the contract tree (`git_open_pr_contract_tree`); implement removes the file |
| `CONFTEST_VEIL_MARKER` | ticket | refuse the merge on the composed contract run |

Every delivered `slugify.py` carries a `# delivered-on: <branch>` line. Without it, the first merge into the test repository would make every later delivery an empty diff. Stamp new canned content too.

The mock does not simulate reasoning, refusal, streaming, sampling, latency or rate limits.

## Invariants

- Every marker is mapped to a carrier, a script and a platform check: `test_scenarios.py::test_every_marker_is_mapped`.
- The mock reads the header the host writes: `test_header_pin.py::test_the_mock_reads_the_header_the_host_writes`.
- Titles, trailers and document headers match the platform's: `test_contract_vocabulary.py`.
- The canned code passes the canned tests: `test_brain.py::test_canned_slugify_satisfies_the_task`.
- Both surfaces report cost in the LiteLLM header: `test_responses.py::test_both_surfaces_report_their_cost_in_the_litellm_header`.

## Testing

```bash
uv run pytest packages/mock-brain
```

See [Contributing](../../docs/developing/contributing.md). No cluster is needed. `test_contract.py` runs the real agent host over HTTP against the mock on a loopback port, through both wire surfaces. The [acceptance suite](../../docs/developing/acceptance.md) refuses a paid brain unless `POSEIDON_ALLOW_PAID=1` is set.

## Related

- [Use a paid brain](../../docs/how-to/paid-brain.md), [Security model](../../docs/explanation/security-model.md), [Merge gate](../../docs/explanation/merge-gate.md)
- [agent-host](../agent-host/README.md)
