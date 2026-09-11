# poseidon-cli

The `poseidon` command line, and `ConsoleClient`, the library it is built
on. Both are thin clients of [the console](../console/README.md): they
hold a human's token and the console's address, and perform no act of
their own. Every act is the console's — decided against the company's
compiled bundle and audited under the person's name there — and what the
CLI prints is what the console answered.

Two commands touch no console: `login` (below) and `blueprint new`, which
writes a document on your disk by asking you questions.

The one thing the CLI does with cluster credentials is `poseidon login`:
minting the token, through the TokenRequest API with the kubeconfig the
caller already holds, for a human the chart declared — printed, or with
`--save` kept at `~/.config/poseidon/token` (owner-readable only), where
every command reads it when `POSEIDON_TOKEN` is unset. The environment
wins over the file.

```bash
export POSEIDON_CONSOLE_URL=http://127.0.0.1:8090       # the kind default
uv run poseidon login owner --save                       # a token for the console's audience, kept

uv run poseidon whoami
uv run poseidon blueprint new                            # a blueprint of your own, by answering questions
uv run poseidon blueprint preview examples/hello-saas.yaml
uv run poseidon blueprint apply examples/hello-saas.yaml --yes

uv run poseidon signal <run_id> merge                    # approve a parked merge
uv run poseidon signal <run_id> merge --no-approved      # reject it
uv run poseidon signal <run_id> budget                   # let a run continue past its pause
uv run poseidon status <run_id>
uv run poseidon story <run_id>
uv run poseidon heartbeat state
uv run poseidon run 42                                   # a run by hand on ticket #42
uv run poseidon review 41                                # or on pull request #41
uv run poseidon follow                                   # watch every row land, and the spend move
uv run poseidon follow --run <run_id>                    # one run's rows
uv run poseidon company delete --reason "why"            # the one stop: cancel runs, delete the company
```

Every command's `--help` is rendered into [the CLI reference](../../docs/reference/cli.md)
at docs build time; the routes beneath them are on [the console reference](../../docs/reference/console.md).

## Module map

| Path | Owns |
|---|---|
| `client.py` | `ConsoleClient`: one method per console route; `PolicyDenied` (403, with the rule) and `ConsoleError` (everything else). |
| `new.py` | `blueprint new`: the questionnaire (a small prompter protocol; questionary on a terminal, a script in tests), the document it builds, the file it renders, the compile it runs first. |
| `main.py` | The typer app: `login`, `whoami`, `blueprint new\|preview\|apply`, `run`, `review`, `signal`, `cancel`, `status`, `story`, `spend`, `follow`, `heartbeat state\|wake`, `company delete`. |
| `follow.py` | The follower: one line per audit row, the serial id as the cursor, spend when it moves; `run` follows its own run through it. |

## Tests

```
uv run pytest packages/cli
```

Against a scripted console (`httpx.MockTransport`): every call carries
the token, a policy refusal surfaces its rule, apply previews first, a
missing token says how to get one, and `login` asks the API server for a
token bound to the console's audience.
