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
what `kind-up.sh` leaves you with, valid for a week, so that applying a
company is the first command you type. A week-long token on
disk is a kind convenience for a token good at the console and nowhere
else; on a cluster with an identity provider `login` does not apply.

```bash
uv run poseidon login owner --save                       # or: export POSEIDON_TOKEN=$(uv run poseidon login owner)
uv run poseidon whoami
```

`blueprint new` is the second command that is not a console call: it
writes a document on your disk by asking you questions, section by
section, with the shipped example's answers as defaults, and compiles it
before writing. Nothing is decided by it; applying is.

```bash
uv run poseidon blueprint new                            # <company-name>.yaml in the current directory
uv run poseidon blueprint new company.yaml --force       # a path of your own; overwrite
```

The human's acts are the charter and the gates:

```bash
uv run poseidon blueprint apply company.yaml --yes       # the charter: vision, caps, gates, humans; returns once converged
uv run poseidon verdict <run_id> merge approve --reason "…"   # approve a parked merge; the reason is recorded with the verdict
uv run poseidon verdict <run_id> merge reject --reason "…"    # reject it
uv run poseidon verdict <run_id> budget approve --reason "…"  # let a run stopped at its cap continue
```

`apply` returns once the company is **converged** — the operator's word
that OPA serves the policy this apply compiled — printing `activating …`
while it waits and `converged (Ns)` when it is; `--no-wait` returns after
the submit, and a company still activating at `--wait-timeout` exits
non-zero and says where to look. `company show` prints the same phase,
generation and bundle revision from the Company CR at any time.

A verdict is recorded under your name before it is delivered, and the
release reads the record: the same command by a person the blueprint's
`company.humans` does not name is refused by policy, with the rule in the
answer.

`start implement` and `start review` start one run by hand — an operator
reproducing a run on a ticket the company filed or a pull request it
delivered. They are not how work enters: nothing does, except the
company's own documents. A run is addressed by its id alone; the run
record knows its workflow.

```bash
uv run poseidon start implement 42                       # implement ticket #42, by hand, and follow it
uv run poseidon start review <pr-number> --no-wait       # review a PR by hand; it parks on the gate
uv run poseidon runs                                     # the company's runs
uv run poseidon run show <run_id>                        # one run's record and its execution
uv run poseidon run story <run_id>                       # its whole audit story
uv run poseidon run story <run_id> --decisions           # only what was decided, and why
uv run poseidon run cancel <run_id> --reason "…"         # stop it
uv run poseidon spend                                    # the company's spend to date; `spend <run_id>` for one run
uv run poseidon heartbeat show                           # the standing loop, and its ticks
```

`follow` is the window: the company's audit rows as they land, one line
each — the time, the run (or `heartbeat`), who acted, the tool, the
decision, and the rule when a denial or a tick names one — with the
spend to date printed whenever it moves, until you interrupt. It reads
the same ledger `story` reads, by the row's serial id, so an interrupted
follow resumed with `--after` repeats nothing and skips nothing; `--run`
narrows to one run, and `--json` gives the rows as stored. `start
implement` and `start review` without `--no-wait` follow their own run
this way.

```bash
uv run poseidon follow                                   # everything, as it happens
uv run poseidon follow --run <run_id>                    # one run, with its own spend
```

Stopping the company is deleting it, and that too is a console act:
`company delete` is decided against `company.humans`, audited under your
name, cancels every running run first, then removes the Company and
waits for the teardown. The repository and the ledgers stay, so applying
the same blueprint again resumes the company.

```bash
uv run poseidon company delete --reason "wrong vision"   # asks first; --yes skips the question
```

Every command's `--help`, rendered from the CLI itself at build time:

<!-- gen: cli -->
