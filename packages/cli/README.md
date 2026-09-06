# poseidon-cli

The `poseidon` command line, and `ConsoleClient`, the library it is built
on. Both are thin clients of [the console](../console/README.md): they
hold a human's token and the console's address, and perform no act of
their own. Every act is the console's — decided against the company's
compiled bundle and audited under the person's name there — and what the
CLI prints is what the console answered.

The one thing the CLI does with cluster credentials is `poseidon login`:
minting the token, through the TokenRequest API with the kubeconfig the
caller already holds, for a human the chart declared.

```bash
export POSEIDON_CONSOLE_URL=http://127.0.0.1:8090       # the kind default
export POSEIDON_TOKEN=$(uv run poseidon login owner)     # a token for the console's audience

uv run poseidon whoami
uv run poseidon blueprint preview examples/hello-saas.yaml
uv run poseidon blueprint apply examples/hello-saas.yaml --yes

uv run poseidon signal <run_id> merge                    # approve a parked merge
uv run poseidon signal <run_id> merge --no-approved      # reject it
uv run poseidon signal <run_id> budget                   # raise a cap a run stopped at
uv run poseidon status <run_id>
uv run poseidon story <run_id>
uv run poseidon heartbeat state
uv run poseidon run 42                                   # an operator's repro on ticket #42
uv run poseidon review 41                                # or on pull request #41
```

Every command's `--help` is rendered into [the CLI reference](../../docs/reference/cli.md)
at docs build time; the routes beneath them are on [the console reference](../../docs/reference/console.md).

## Module map

| Path | Owns |
|---|---|
| `client.py` | `ConsoleClient`: one method per console route; `PolicyDenied` (403, with the rule) and `ConsoleError` (everything else). |
| `main.py` | The typer app: `login`, `whoami`, `blueprint preview\|apply`, `run`, `review`, `signal`, `cancel`, `status`, `story`, `spend`, `heartbeat state\|wake`. |

## Tests

```
uv run pytest packages/cli
```

Against a scripted console (`httpx.MockTransport`): every call carries
the token, a policy refusal surfaces its rule, apply previews first, a
missing token says how to get one, and `login` asks the API server for a
token bound to the console's audience.
