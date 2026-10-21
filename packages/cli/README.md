# poseidon-cli

`poseidon` is the command-line client of the [console](../console/README.md), and `ConsoleClient` is the library beneath it. You run the CLI from a checkout with `uv run poseidon`, and the acceptance suite imports `ConsoleClient`.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| CLI and library | none | `poseidon` (`poseidon_cli.main:app`) | a human's console token | nothing |

Every command, with its options and examples, is on the [CLI reference](../../docs/reference/cli.md), rendered from the commands' `--help`. Change a command's docstring or `epilog` to change its entry there.

## Module map

| Module | Owns |
|---|---|
| `client.py` | `ConsoleClient`: one method per console route. `PolicyDenied` for a 403 with a rule, `ConsoleError` for any other refusal. The token and URL lookup. |
| `main.py` | The typer app and every command. `login`, `version` and `blueprint new` are the commands that work without a token. |
| `render.py` | How the CLI prints: a record, a table, an act, an error, and `row`, the one line an audit row becomes. |
| `follow.py` | The cursor poll behind `poseidon follow` and behind `start`, which follows its own run. |
| `new.py` | `blueprint new`: the questionnaire, the document it builds, the compile it runs before writing. |
| `__init__.py` | Exports `ConsoleClient`, `ConsoleError` and `PolicyDenied`. |

## How it works

### Every act is one console call

The CLI performs no act of its own. `_client()` reads the console's address from `POSEIDON_CONSOLE_URL` (default `http://127.0.0.1:8090`). It reads the token from `POSEIDON_TOKEN`, or else from `$XDG_CONFIG_HOME/poseidon/token` (default `~/.config/poseidon/token`). Without a token, a command says how to get one and exits 2.

`_call` turns a refusal into one line on stderr, with the policy rule beneath it, and exits 1. When the console refuses a saved token, it adds that `poseidon login <name> --save` writes a fresh one.

`login` is the one command that uses cluster credentials. With your kubeconfig, it asks the TokenRequest API for a token for `human-<name>` in the platform namespace, bound to the console's audience. `--save` writes it with mode 0600.

`blueprint apply` previews the diff, asks unless `--yes`, and applies. It then polls `/company` until the phase is `converged` for the generation the apply made. `--no-wait` skips the wait, and `--wait-timeout` bounds it (600 seconds by default).

`blueprint new` asks through a small `Prompter` protocol: questionary in a terminal, a script in tests. Its defaults are the shipped example's answers. The vision and the repository have no default. It compiles the document before writing it and refuses to overwrite a file without `--force`.

### Rendering has four shapes

`render.py` prints a record (label and value), a table, an act (one sentence) or an error (stderr). Color lives only on status words. `rich` turns it off when stdout is not a terminal or `NO_COLOR` is set. `--json` on every read prints the console's answer verbatim instead.

`render.row` is the one line an audit row becomes. It shows the time, the run (or `heartbeat`), the role and agent, the tool and the decision. The rule follows a denial or a heartbeat row. A rationale follows on its own line. `follow` and `run story` print rows this way.

### `follow` is a cursor poll

`follow.follow` asks `/events` for the rows after a cursor every two seconds. The cursor is the row's serial id, so no row prints twice or is skipped. On interrupt, it prints the cursor to resume from with `--after`. A spend line prints on the first poll and whenever the total moves. With `--json`, each row prints as one JSON object per line.

`start implement` and `start review` follow their own run's rows to stderr until the run ends. They exit 1 unless the run is `done`.

## Invariants

- Every call carries the token: `test_cli.py::test_every_call_carries_the_token`.
- A policy refusal names its rule: `test_cli.py::test_a_policy_refusal_names_the_rule`, `test_a_denied_verdict_exits_with_the_rule`.
- The environment wins over the saved token: `test_cli.py::test_the_client_falls_back_to_the_saved_token`.
- A missing token says how to get one: `test_cli.py::test_without_a_token_the_cli_says_how_to_get_one`.
- `login` asks for a console-audience token: `test_cli.py::test_login_asks_the_api_server_for_a_console_token`.
- `apply` previews first and waits for `converged`: `test_cli.py::test_apply_previews_first_and_applies_with_yes`, `test_apply_waits_for_converged_and_says_so`.
- `follow` prints each row once and resumes from the cursor: `test_follow.py::test_follow_prints_each_row_once_and_resumes_from_the_cursor`.
- Spend prints only when it moves: `test_follow.py::test_spend_prints_once_and_then_only_when_it_moves`.
- The questionnaire's defaults are the shipped example, and what it writes compiles: `test_new.py::test_the_defaults_are_the_shipped_example`, `test_what_is_written_is_what_was_built`.

## Testing

```bash
uv run pytest packages/cli
```

It needs no cluster or console (see [Contributing](../../docs/developing/contributing.md)). The console is an `httpx.MockTransport`, the commands run through typer's `CliRunner`, and the questionnaire runs against a script. On kind, `test_acceptance.py` and `test_review_gate.py` run `uv run poseidon`, and the suite's `cluster.py` helpers act through `ConsoleClient`.

## Related

- [CLI reference](../../docs/reference/cli.md), [Console reference](../../docs/reference/console.md)
- [console](../console/README.md)
- [Gates and runs](../../docs/how-to/gates-and-runs.md), [Write a blueprint](../../docs/how-to/write-a-blueprint.md)
