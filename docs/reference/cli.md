# The `poseidon` CLI

The CLI is a **thin client of [the console](console.md)**. It holds a
human's token and the console's address (`POSEIDON_CONSOLE_URL`, on kind
`http://127.0.0.1:8090`), and performs no act of its own: every act is the
console's, decided against the company's compiled bundle and audited
under the person's name there, and what the CLI prints is what the
console answered — rendered for a person by default, verbatim with
`--json` on every read. It reaches no workflow engine and no database.
The one thing it does with cluster credentials is `login`: minting the
token, through the TokenRequest API with the kubeconfig you already hold,
for a human the chart declares. The token is read from `POSEIDON_TOKEN`,
or, when that is unset, from the file `login --save` keeps in your config
directory (`~/.config/poseidon/token`, owner-readable only) — which is
what `kind-up.sh` leaves you with, valid for a week, so that applying a
company is the first command you type. A week-long token on disk is a
kind convenience for a token good at the console and nowhere else; on a
cluster with an identity provider `login` does not apply.

```bash
uv run poseidon login owner --save                       # or: export POSEIDON_TOKEN=$(uv run poseidon login owner)
uv run poseidon whoami
```

```text
you are owner (human)
company: hello-saas · converged
```

## The tree

The commands are the glossary's nouns — the blueprint, the company, its
heartbeat, its runs — with one verb per meaning: `show` reads, `start`
begins a run by hand, `verdict` answers a gate, `pause` and `resume` hold
the company and let it go. Every act that owes a rationale (a verdict, a
cancel, a pause, a resume, a delete — the acts table on
[the console](console.md)) takes `--reason`, recorded with it under your
name; every command that would wait for the cluster takes `--no-wait`.

```text
poseidon login <name> [--save]
poseidon whoami
poseidon version
poseidon blueprint new | preview | apply [--no-wait]
poseidon company show | delete --reason [--no-wait]
poseidon heartbeat show | wake
poseidon pause --reason | resume --reason
poseidon runs
poseidon run show | story [--decisions] | cancel --reason   <run_id>
poseidon start implement <ticket> | review <pr>  [--no-wait]
poseidon verdict <run_id> merge|budget approve|reject --reason
poseidon follow [--run <run_id>]
poseidon spend [<run_id>]
```

## Writing and applying a blueprint

`blueprint new` is the second command that is not a console call: it
writes a document on your disk by asking you questions, section by
section — the company, the repository, who decides, the money — and
compiles it before writing. Nothing is decided by it; applying is.

```bash
uv run poseidon blueprint new                            # <company-name>.yaml in the current directory
uv run poseidon blueprint new company.yaml --force       # a path of your own; overwrite
uv run poseidon blueprint preview company.yaml           # the diff against what the cluster hosts; changes nothing
uv run poseidon blueprint apply company.yaml --yes       # the charter: vision, caps, gates, humans; returns once converged
```

`apply` returns once the company is **converged** — the reconciler's word
that OPA serves the policy this apply compiled — printing `activating …`
while it waits and `converged (12 s)` when it is; `--no-wait` returns
after the submit, and a company still activating at `--wait-timeout`
exits non-zero and says where to look. `company show` prints the same
phase, generation and bundle revision from the Company CR at any time.

## Answering a gate

A verdict is recorded under your name, with your reason, before it is
delivered, and the release reads the record: the same command by a person
the blueprint's `company.humans` does not name is refused by policy, with
the rule in the answer.

```bash
uv run poseidon verdict <run_id> merge approve --reason "…"   # approve a parked merge
uv run poseidon verdict <run_id> merge reject --reason "…"    # reject it
uv run poseidon verdict <run_id> budget approve --reason "…"  # let a run parked at its cap continue
```

```text
verdict recorded: merge approved for run auto-86d8e022, by owner
```

## Reading the company

`runs` lists the company's runs, `run show` one run's record, `run story`
its whole audit story from every source, and `run story --decisions` only
the rows that carry a rationale — what was decided, and why. On the
heartbeat's own run (`heartbeat-<company>`) that is the company's diary.
`spend` is the company's spend to date; `spend <run_id>` one run's, call
by call.

```bash
uv run poseidon runs
uv run poseidon run show <run_id>
uv run poseidon run story <run_id> --decisions
uv run poseidon spend
```

```text
run             workflow    task                        status   started      took
────────────────────────────────────────────────────────────────────────────────────
auto-9a326f8c   scope       scope the next slice        done     4 min ago    5 s
auto-86d8e022   review      review pull request #1949   done     6 min ago    1 min 19 s
run-01M2E6RZ…   implement   implement issue #1948       done     7 min ago    48 s
```

```text
run           auto-86d8e022
workflow      review
task          review pull request #1949
status        done
started       6 min ago
took          1 min 19 s
pull request  https://github.com/example-org/hello-saas/pull/1949
verdict       pass
execution     workflow COMPLETED review-auto-86d8e022
```

```text
20:20:43  auto-86d8e022   platform/orchestrator   review.verdict  allow
          Suite green in the review sandbox; change matches the acceptance criteria.
20:21:15  auto-86d8e022   human/owner             gate.resolve  allow
          reviewed: findings are clean
```

Every read prints the console's answer as JSON with `--json`; scripts and
the acceptance suite read that, never the rendering.

## Starting a run by hand

`start implement` and `start review` start one run by hand — a person
reproducing a run on a ticket the company filed or a pull request it
delivered — and follow it to its end, printing its rows as they land and
then its record; `--no-wait` returns after the start. They are not how
work enters: nothing does, except the company's own documents. A run is
addressed by its id alone; the run record knows its workflow.

```bash
uv run poseidon start implement 42                       # implement ticket #42, by hand, and follow it
uv run poseidon start review <pr-number> --no-wait       # review a PR by hand; it parks on the gate
uv run poseidon run cancel <run_id> --reason "…"         # stop a run; recorded under your name
```

## Watching it work

`follow` is the window: the company's audit rows as they land, one line
each — the time, the run (or `heartbeat`), who acted, the tool, the
decision, the rule when a denial or a tick names one, and the actor's
rationale beneath when one was stated — with the spend to date printed
whenever it moves, until you interrupt. It reads the same ledger `run
story` reads, by the row's serial id, so an interrupted follow resumed
with `--after` repeats nothing and skips nothing; `--run` narrows to one
run, and `--json` gives the rows as stored. `heartbeat show` is the
standing loop itself: its execution, and every tick with what it started
or why it did nothing; `heartbeat wake` asks it to look now.

```bash
uv run poseidon follow                                   # everything, as it happens
uv run poseidon follow --run <run_id>                    # one run, with its own spend
uv run poseidon heartbeat show
```

## Pausing the company, and stopping it

`pause` holds the company still without ending it: the heartbeat starts
nothing until `resume`, and everything stays where it is to be read —
the runs, the spend, the story, the agents. Runs already in flight
finish; you may still start a run, answer a gate or cancel by hand. Both
are console acts under your name with your reason; `company show` says
who holds the heartbeat, `heartbeat show` says `paused`, and every
paused tick is on the record with your name.

```bash
uv run poseidon pause --reason "reading the ledger with nothing moving"
uv run poseidon company show                             # heartbeat   paused by owner, 2 min ago
uv run poseidon resume --reason "read it"
```

Stopping the company is deleting it, and that too is a console act:
`company delete` is decided against `company.humans`, audited under your
name with your reason, cancels every running run first, then removes the
Company and waits for the teardown. The repository and the ledgers stay,
so applying the same blueprint again brings the company back.

```bash
uv run poseidon company delete --reason "wrong vision"   # asks first, naming the runs it will cancel; --yes skips the question
```

Every command's `--help`, rendered from the CLI itself at build time:

<!-- gen: cli -->
