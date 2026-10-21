# Build a game with a paid brain

In this tutorial you give a company the vision of a small Tetris game.
You watch it write its strategy, plan the first slice of work, write the
code, review it, and wait for you to merge it.

!!! warning "This uses a paid brain"
    Every agent call is billed to your API key. The example blueprint caps
    each run at $5 and each day at $20. Lower `budgets.per_run.usd` and
    `heartbeat.daily_cap.usd` if you want tighter caps.

## Before you start

- Finish the [Quickstart](quickstart.md), so you have a working cluster and
  a disposable repository.
- Have an API key from OpenAI or Anthropic.
- If the Quickstart's company is still applied, delete it:
  `uv run poseidon company delete --reason "starting the tutorial" --yes`.

## 1. Switch to a paid brain

Set the model in `deploy/kind/.env` and bring the cluster up again:

```bash
REAL_LLM=1
LLM_API_KEY=sk-your-provider-key
MODEL=gpt-5.6-luna
```

```bash
deploy/kind/kind-up.sh
```

The first step of the output now says `brain: PAID`.
[Use a paid brain](../how-to/paid-brain.md) lists the other providers.

## 2. Write the vision

Copy the example blueprint again:

```bash
cp examples/hello-saas.yaml company.yaml
```

In `company.yaml`, set `stack.repo.url` to your disposable repository, as
in the Quickstart. Then replace the vision:

```yaml
company:
  name: hello-saas
  vision: >-
    A playable, self-contained Tetris in Python, standard library only,
    built engine first — board, tetrominoes, gravity, line clears, a
    score — with pytest coverage, on a repository the company keeps green.
```

The vision is the only instruction the company gets. It works out
everything else from it.

## 3. Apply it and watch

```bash
uv run poseidon blueprint apply company.yaml --yes
```

Open a second terminal and follow the company. Leave it running for the
rest of the tutorial:

```bash
uv run poseidon follow
```

Every tick, run, tool call, decision and verdict prints as one line, and
the spend prints each time it moves.

## 4. Approve the strategy and the architecture plan

The repository has no strategy and no architecture plan yet. The
company's first move is to file two tickets on your repository:
`Bootstrap: strategy` and `Bootstrap: architecture masterplan`. An agent
called the *steward* writes each document from your vision and opens a
pull request. A reviewer agent reads it, and the run parks for your
verdict.

Read each pull request on GitHub. If the strategy describes work you want,
approve it with the review run's id from `poseidon runs`:

```bash
uv run poseidon verdict <run_id> merge approve --reason "the strategy names a slice I want built"
```

The company won't plan any work until both documents are on `main`.

## 5. Watch it plan the work

With the documents merged, the PM agent reads them and files the first
slice as tickets on your repository, for example the game engine. Each
ticket names the crew that will build it, and the PM's summary is its
first comment.

The PM files tickets and nothing else. It can't write code. The company
stops planning while `heartbeat.max_open_tickets` tickets are open.

## 6. Watch it build a slice

On a later tick the PM picks a ticket, and the company starts an
*implement* run on it:

1. A short-lived implementer agent gets its own sandbox with a copy of the
   repository, and writes the code.
2. The platform, not the agent, runs `pytest -q` in the sandbox. If the
   tests fail, the implementer gets the failure and tries again, up to
   `max_verify_retries` times.
3. Only when the tests pass does the agent get the tool that opens a pull
   request. The pull request comes from a `feature/…` branch.

## 7. Review and merge

The new pull request starts a *review* run. A separate reviewer agent
reads the diff, runs the tests in its own sandbox, posts its findings on
the pull request, and gives a pass or fail verdict. Then the run parks.

Read the findings and the code, then answer:

```bash
uv run poseidon verdict <run_id> merge approve --reason "engine first, as the vision asks"
```

The pull request merges and its ticket closes. The company moves on to
the next ticket.

## 8. Read the record

Everything the company did is recorded. To see what one run decided and
why, including your verdict:

```bash
uv run poseidon run story <run_id> --decisions
```

To see what the company spent:

```bash
uv run poseidon spend
```

## 9. Stop or keep going

The company keeps working through its tickets within its caps. To stop it
starting new work, pause it:

```bash
uv run poseidon pause --reason "done for today"
```

When you are done, delete it with `poseidon company delete`.

## Next steps

- [Delegate merges](../how-to/delegate-merges.md): let the company merge its
  own passing work in the paths you choose.
- [How it works](../explanation/how-it-works.md): what contains each agent.
- [Blueprint reference](../reference/blueprint.md): every field you can set.
