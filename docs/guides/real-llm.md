# Use a real model

The mock brain is what the acceptance suite is written against. To run
`hello-saas` (or any other example company) with a real model, the cluster
must be brought up with `REAL_LLM=1`. Nothing does that for you.

The provider key never enters an agent pod. It lands only in the
`llm-proxy`, behind the LLM gateway; LiteLLM is started in single-model
mode, so every role — PM, implementer, reviewer — is served the
same `MODEL`.

Edit `deploy/kind/.env` (create it from `.env.example` if you have not
already). Uncomment and fill:

```bash
REAL_LLM=1
LLM_API_KEY=sk-your-provider-key
MODEL=gpt-5.6-luna
```

`gpt-5.6-luna` is the cheap OpenAI default this repo is wired for. Then
re-run the bootstrap so the proxy, the secret, and the gateway's pinned
model all change together:

```bash
deploy/kind/kind-up.sh
```

To compare against Anthropic, swap **both** the key and the model —
LiteLLM copies `LLM_API_KEY` into `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
and reads the one that matches the model prefix, so leaving an OpenAI key
in place while changing `MODEL` to Claude will fail:

```bash
REAL_LLM=1
LLM_API_KEY=sk-ant-your-anthropic-key
MODEL=anthropic/claude-sonnet-4-5
```

Re-run `kind-up.sh` after every provider or model change; the company
stays applied and its agents re-roll. The same file
holds `POSEIDON_CONSOLE_URL`, where the `poseidon` CLI reaches the console
(`http://127.0.0.1:8090` on kind); a provider change does not touch it.

## Which models work

The agent host picks the wire shape (OpenAI Responses or chat
completions) and whether to send a reasoning effort from a table by
model **family**, `agent.models`, so a new release in a known family
needs no change anywhere. Poseidon speaks one reasoning knob,
`reasoning_effort`; LiteLLM translates it per provider. A model no row
names keeps the OpenHands SDK's own guess, as the mock brain does.

<!-- gen: models -->

The provider prefix (`openai/`, `anthropic/`) routes the call and is not
part of the match. One more thing has to know the model: the `llm-proxy`
must price it, because the gateway meters the cost LiteLLM reports and
withholds a call it cannot price (`llm_cost_unreported`). LiteLLM fetches
its price table when it starts, so the pinned image usually knows a
model newer than itself; if a run fails on that error, bump
`images.llmProxy.tag` in the chart values.

!!! warning "The acceptance suite refuses a paid brain"
    Do not point `scripts/acceptance.sh` at a paid provider: the suite
    asserts mock-brain behaviour, not model quality, and against a live
    model it would spend money — so it refuses to run. Both the script's
    preflight and the suite's own session guard read the *deployed*
    `llm-proxy` image (never `.env`'s `REAL_LLM` claim) and fail fast on a
    paid brain unless `POSEIDON_ALLOW_PAID=1` deliberately opts into
    spending.

Once the real model is up, the walkthrough in
[Getting started](getting-started.md#a-full-run-a-small-tetris-app) is the
full SDLC.
