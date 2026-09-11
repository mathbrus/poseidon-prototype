# The `poseidon` CLI

The CLI is a **thin client of [the console](console.md)**. It holds a
human's token and the console's address (`POSEIDON_CONSOLE_URL`, on kind
`http://127.0.0.1:8090`), and performs no act of its own: every act is the
console's, decided against the company's compiled bundle and audited
under the person's name there, and what the CLI prints is what the
console answered. It reaches no workflow engine and no database. The one
thing it does with cluster credentials is `login`: minting the token,
through the TokenRequest API with the kubeconfig you already hold, for a
human the chart declares. The token is read from `POSEIDON_TOKEN`, or,
when that is unset, from the file `login --save` keeps in your config
directory (`~/.config/poseidon/token`, owner-readable only) — which is
what `kind-up.sh` leaves you with, valid for a week. A week-long token on
disk is a kind convenience for a token good at the console and nowhere
else; on a cluster with an identity provider `login` does not apply.

```bash
uv run poseidon login owner --save                       # or: export POSEIDON_TOKEN=$(uv run poseidon login owner)
uv run poseidon whoami
```

The human's acts are the charter and the gates:

```bash
uv run poseidon blueprint apply company.yaml --yes       # the charter: vision, caps, gates, humans
uv run poseidon signal <run_id> merge                    # approve a parked merge
uv run poseidon signal <run_id> merge --no-approved      # reject it
uv run poseidon signal <run_id> budget                   # raise a cap the run stopped at
```

A verdict is recorded under your name before it is delivered, and the
release reads the record: the same command by a person the blueprint's
`company.humans` does not name is refused by policy, with the rule in the
answer.

`run` and `review` start one run by hand — an operator reproducing a run
on a ticket the company filed or a pull request it delivered. They are not
how work enters: nothing does, except the company's own documents. A run
is addressed by its id alone; the run record knows its workflow.

```bash
uv run poseidon run 42                                   # implement ticket #42, by hand
uv run poseidon review <pr-number> --detach              # review PR by hand; park on the gate
uv run poseidon status <run_id>                          # the record and its execution
uv run poseidon story <run_id>                           # the whole audit story
uv run poseidon heartbeat state                          # the standing loop, and its ticks
```

Every command's `--help`, rendered from the CLI itself at build time:

<!-- gen: cli -->
