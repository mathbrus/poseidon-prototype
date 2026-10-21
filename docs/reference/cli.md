# CLI

`poseidon` is the command-line client of the [console](console.md). Every
command is a console call, made under your name, except `login` and
`blueprint new`, which run on your machine. The CLI needs no database
address, no port-forward and no cluster credentials, apart from `login`.

Run it from the repository with `uv run poseidon …`.

## Signing in

The CLI reads its token from `POSEIDON_TOKEN`, or, when that's unset,
from `~/.config/poseidon/token`. `kind-up.sh` saves a week-long token there
for `owner`, so you are signed in after bring-up.

```bash
uv run poseidon login owner --save                        # save a fresh token
export POSEIDON_TOKEN=$(uv run poseidon login auditor)    # act as someone else
```

It reaches the console at `POSEIDON_CONSOLE_URL`, or
`http://127.0.0.1:8090` when that's unset.

## Conventions

- **`--reason`** is required on every act that commits the company to
  something: a verdict, a cancel, a pause, a resume, a delete. It is
  recorded with the act, under your name.
- **`--json`** on a read prints the console's answer exactly as stored.
  Scripts should read that, not the rendered text.
- **`--no-wait`** returns as soon as the console accepts the request,
  instead of waiting for the cluster.

## Commands

<!-- gen: cli -->
