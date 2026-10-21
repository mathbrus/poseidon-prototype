# Use a paid brain

The *brain* is the language model behind the LLM gateway. The cluster runs
the free, deterministic mock brain by default. This guide switches it to a
paid brain, a model from OpenAI or Anthropic, so the company does real
work.

!!! warning "This spends money"
    Every agent call goes to your provider and is billed to your key. The
    blueprint's `budgets.per_run.usd` and `heartbeat.daily_cap.usd` cap
    what the company spends, so set them before you start.

## Before you start

- A working mock cluster, as in the [Quickstart](../get-started/quickstart.md).
- An API key from your provider.

## Steps

### 1. Set the provider in `deploy/kind/.env`

Uncomment and fill these three lines:

```bash
REAL_LLM=1
LLM_API_KEY=sk-your-provider-key
MODEL=gpt-5.6-luna
```

Pick the key and model from one row. The key must match the model's
provider:

| Provider | `LLM_API_KEY` | `MODEL`, for example |
|---|---|---|
| OpenAI | `sk-…` | `gpt-5.6-luna` (the cheap default) |
| Anthropic | `sk-ant-…` | `anthropic/claude-sonnet-4-5` |

Every agent uses the same model.

### 2. Bring the cluster up again

```bash
deploy/kind/kind-up.sh
```

The script redeploys the model proxy with your key. Your company stays
applied, and its agents restart on the new model. The first step of the
output names the model, marked as paid:

```text
      brain: PAID — gpt-5.6-luna through LiteLLM (REAL_LLM=1)
```

Run `kind-up.sh` again after every change of provider or model.

## Check that it worked

Follow the company and watch the spend move:

```bash
uv run poseidon follow
```

Each model call adds to the spend line. `uv run poseidon spend` prints
the total to date.

## Undo it

Set `REAL_LLM=0` in `deploy/kind/.env`, or run
`REAL_LLM=0 deploy/kind/kind-up.sh`. A value on the command line wins
over the file.

## Which models work

The agent host knows how to talk to each model family below. A newer
model in a known family works without any change.

<!-- gen: models -->

Write the provider prefix (`openai/`, `anthropic/`) in `MODEL` if you like.
It doesn't affect the match.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Runs fail with `llm_cost_unreported` | The model proxy can't price the model, and the LLM gateway refuses calls it can't meter. Raise `images.llmProxy.tag` in the chart values to a newer LiteLLM release, then run `kind-up.sh`. |
| Every call fails after you changed `MODEL` | The key belongs to another provider. Change `LLM_API_KEY` and `MODEL` together. |
| `scripts/acceptance.sh` refuses to start | The acceptance suite refuses a paid brain, so it can't spend money. Bring the cluster up with `REAL_LLM=0` to run it. |

## Next steps

- [Build a game with a paid brain](../get-started/tutorial.md)
- [Troubleshooting](troubleshooting.md)
