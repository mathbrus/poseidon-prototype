# Troubleshooting

Look up the error you see. Each row gives the cause and the fix.

## Bringing up the cluster

| You see | Cause and fix |
|---|---|
| `kind-up.sh` stops at a numbered step | The step's tools failed. The script prints the tail of `deploy/kind/kind-up.log`, and the full output is in that file. Run with `VERBOSE=1` to stream everything. |
| `warning: … GiB free under /var/lib/docker` | The cluster and the images need room. Free space with `docker image prune` and check with `docker system df`. |
| `deploy/kind/.env not found` | Copy `deploy/kind/.env.example` to `deploy/kind/.env` and set `GIT_TOKEN`. |
| A pod runs old code after you changed it | Run `kind-up.sh` again. It rebuilds the images and checks that every platform pod runs the new build. |

## Signing in

| You see | Cause and fix |
|---|---|
| `no token: poseidon login <name> --save keeps one at …` | No token is saved and `POSEIDON_TOKEN` is unset. Run `uv run poseidon login owner --save`. |
| `the saved token at … was refused — expired or revoked` | Tokens saved by `kind-up.sh` last a week. Run `uv run poseidon login owner --save` for a fresh one. |
| `console unreachable at http://127.0.0.1:8090` | The cluster is down, or `POSEIDON_CONSOLE_URL` points elsewhere. Run `kind-up.sh`. |
| `human_not_recognized` | Your name is not in the blueprint's `company.humans`. Add it and apply the blueprint again, as someone it already names. |

## Applying a blueprint

| You see | Cause and fix |
|---|---|
| `this cluster hosts 'a'; delete it before applying 'b'` | A cluster hosts one company. Run `uv run poseidon company delete --reason "…"` first, or keep the same `company.name`. |
| `company.humans names 'x', whom this console cannot verify` | On kind, a blueprint may only name the people the chart declares: `owner` and `auditor`. |
| A validation error quoting the schema | A field is misspelled, missing or of the wrong type. Unknown keys are errors. Check the [blueprint reference](../reference/blueprint.md). |
| `still activating after 600s` | The policy for the company has not reached OPA. Run `uv run poseidon company show` for the message, and `kubectl get company` for the reconciler's view. |

## Running the company

| You see | Cause and fix |
|---|---|
| Nothing starts | The heartbeat may be paused (`uv run poseidon company show` says by whom), at a cap, or its PM chose `idle`. `uv run poseidon heartbeat show` lists each tick and why it started nothing. |
| A run in state `parked-merge_gate` | It waits for your merge verdict. See [Answer gates and follow a run](gates-and-runs.md). |
| A run in state `parked-budget_exhausted` | The run reached `budgets.per_run.usd`. Approve or reject it with `poseidon verdict <run_id> budget …`. |
| `run 'x' is not parked on its merge gate` | The run is not waiting for a verdict, or already got one. Check its state with `uv run poseidon run show <run_id>`. |
| `repo_not_declared` in a run's story | An agent named a repository other than `stack.repo.url`, and policy refused. Check that `stack.repo.url` is the repository you meant. |
| `llm_cost_unreported` | The model proxy can't price your model. See [Use a paid brain](paid-brain.md#troubleshooting). |

## Running the acceptance suite

| You see | Cause and fix |
|---|---|
| The preflight asks for `E2E_TARGET_REPO` | Name a disposable repository: `E2E_TARGET_REPO=github.com/you/disposable scripts/acceptance.sh`. |
| `this cluster hosts 'x'; the suite applies 'hello-saas'` | Delete the company first: `uv run poseidon company delete --reason "…"`. |
| The preflight refuses a paid brain | The suite only runs against the mock. Bring the cluster up with `REAL_LLM=0 deploy/kind/kind-up.sh`. |
| `gateway at … answered '000'` | The cluster is down. Run `kind-up.sh`. |
