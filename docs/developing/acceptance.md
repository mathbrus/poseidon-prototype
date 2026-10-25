# Run the acceptance suite

The acceptance suite runs Poseidon end to end on a live kind cluster. It
proves every row of [What you get](../index.md#what-you-get), including the
red-team tests that attack a real sandbox and a real merge gate. A full run
takes about an hour.

## Before you start

- A cluster on the mock brain: `REAL_LLM=0 deploy/kind/kind-up.sh`. The
  suite refuses a paid brain, so it never spends money.
- No company applied, or the shipped `hello-saas`. The suite applies
  `hello-saas` itself.
- A **disposable** GitHub repository that `GIT_TOKEN` can write to. The
  suite takes over its `main` branch.

## Run it

```bash
E2E_TARGET_REPO=github.com/<you>/<disposable> scripts/acceptance.sh
```

The script checks the cluster first and fails in seconds if something is
wrong. Then it runs `tests/e2e_kind` in full. pytest's summary gives the
count, and `tests/e2e_kind/test_acceptance.py` indexes which module proves
what. After the summary, a *harness waits* section shows where the suite's
own waiting went: blueprint applies, the GitHub listing, repository
sweeps, and waits for the heartbeat.

## What it does

- Acts only through the console, as `owner`, like a person would. It reads
  the results with `psql`, `kubectl` and the GitHub API.
- Resets the repository's `main` to a known baseline, then opens real pull
  requests and issues on it.
- Closes every pull request, branch and issue it created.
- Ends by checking that the cluster is quiet: no running workflow, no
  unfinished run. You can run it twice in a row with nothing in between.

`tests/e2e_kind/conftest.py` documents these guarantees, and what a new
test module owes them.

## When the preflight refuses

| Message | Fix |
|---|---|
| The repository variable is missing | Set `E2E_TARGET_REPO`. |
| `cluster unreachable via current kubectl context` | Run `deploy/kind/kind-up.sh`. |
| `gateway at … answered '…' (expected 401 without a token)` | The cluster isn't serving. Run `kind-up.sh` again. |
| A paid brain is deployed | Bring the cluster up with `REAL_LLM=0`. `POSEIDON_ALLOW_PAID=1` overrides the check, and spends money. |
| `this cluster hosts '…'; the suite applies 'hello-saas'` | Delete that company: `uv run poseidon company delete --reason "…"`. |

## Clean up

```bash
deploy/kind/kind-down.sh
```
