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

`apply` returns once the company is **converged** — the operator's word
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
uv run poseidon verdict <run_id> budget approve --reason "…"  # let a run stopped at its cap continue
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

### `poseidon`

```text
Usage: poseidon [OPTIONS] COMMAND [ARGS]...

  Act on the company this cluster hosts, and read it, through the console.

Options:
  --help  Show this message and exit.

Commands:
  login      Mint a console token for a human the chart declares, with the...
  whoami     Who the console takes you for, and which company it hosts.
  pause      Hold the company: its heartbeat starts nothing until `resume`,...
  resume     Lift the pause: the heartbeat looks at the world at once and...
  runs       The company's runs, newest first: id, workflow, task, status,...
  verdict    Answer a gate a run parked on.
  follow     Watch the company: every audit row as it lands — ticks, runs,...
  spend      Spend to date: the company's, or one run's with RUN_ID — the...
  blueprint  Write, preview and apply a company blueprint.
  company    The company this cluster hosts: show it, or delete it.
  heartbeat  The company's standing loop: show it, or wake it.
  run        One run: show it, read its story, cancel it.
  start      Start a run by hand, on a ticket or a pull request.
```

### `poseidon login`

```text
Usage: poseidon login [OPTIONS] {name}

  Mint a console token for a human the chart declares, with the kubeconfig you
  already hold (a kind cluster; elsewhere your identity provider signs you in).
  Prints the token, or saves it with --save.

Arguments:
  name  A human the chart declares (on kind: owner, auditor).  [required]

Options:
  --namespace <str>  The namespace the platform is installed into.  [default:
                     poseidon-platform]
  --duration <int>   How long the token lives, in seconds.  [default: 3600]
  --save             Save the token in your config directory instead of printing
                     it; every later command reads it there when POSEIDON_TOKEN
                     is unset.
  --help             Show this message and exit.
```

### `poseidon whoami`

```text
Usage: poseidon whoami [OPTIONS]

  Who the console takes you for, and which company it hosts.

Options:
  --json  Print exactly what the console answered, as JSON.
  --help  Show this message and exit.
```

### `poseidon pause`

```text
Usage: poseidon pause [OPTIONS]

  Hold the company: its heartbeat starts nothing until `resume`, and everything
  stays where it is to be read. Runs already in flight finish; you may still
  start a run, answer a gate or cancel by hand. Recorded under your name with
  your reason.

Options:
  --reason <str>  Why. Recorded with the act, under your name.  [required]
  --help          Show this message and exit.
```

### `poseidon resume`

```text
Usage: poseidon resume [OPTIONS]

  Lift the pause: the heartbeat looks at the world at once and decides for
  itself what to start. Recorded under your name with your reason.

Options:
  --reason <str>  Why. Recorded with the act, under your name.  [required]
  --help          Show this message and exit.
```

### `poseidon runs`

```text
Usage: poseidon runs [OPTIONS]

  The company's runs, newest first: id, workflow, task, status, when.

Options:
  --since <str>  Only runs started at or after this timestamp.
  --json         Print exactly what the console answered, as JSON.
  --help         Show this message and exit.
```

### `poseidon verdict`

```text
Usage: poseidon verdict [OPTIONS] {run_id} {gate}:<merge|budget>
                        {verdict}:<approve|reject>

  Answer a gate a run parked on. The console records the verdict and your reason
  under your name, then delivers it. Approving a budget park lets the run
  continue under its cap as it stands.

Arguments:
  run_id                    The run parked on the gate.  [required]
  gate:<merge|budget>       The gate: merge, or budget.  [required]
  verdict:<approve|reject>  approve or reject.  [required]

Options:
  --reason <str>  Why. Recorded with the act, under your name.  [required]
  --help          Show this message and exit.
```

### `poseidon follow`

```text
Usage: poseidon follow [OPTIONS]

  Watch the company: every audit row as it lands — ticks, runs, tool calls,
  decisions, gates, verdicts — one line each, the spend whenever it moves, until
  you interrupt.

Options:
  --run <str>    Only this run's rows.
  --after <int>  Resume from this cursor (the last id printed).  [default: 0]
  --json         The rows as stored, one JSON object per line.
  --help         Show this message and exit.
```

### `poseidon spend`

```text
Usage: poseidon spend [OPTIONS] [run_id]

  Spend to date: the company's, or one run's with RUN_ID — the total, and every
  model call behind it.

Arguments:
  run_id  A run's id; omit for the company's.

Options:
  --since <str>  Company-wide: only spend at or after this timestamp.
  --json         Print exactly what the console answered, as JSON.
  --help         Show this message and exit.
```

### `poseidon blueprint`

```text
Usage: poseidon blueprint [OPTIONS] COMMAND [ARGS]...

  Write, preview and apply a company blueprint.

Options:
  --help  Show this message and exit.

Commands:
  new      Write a blueprint by answering questions, section by section.
  preview  Show what applying BLUEPRINT would change — the diff against...
  apply    Apply BLUEPRINT: shows the diff, asks, applies through the...
```

### `poseidon blueprint new`

```text
Usage: poseidon blueprint new [OPTIONS] [path]

  Write a blueprint by answering questions, section by section. Compiles the
  answers before writing; prints the file written and the command that applies
  it. Nothing reaches the cluster.

Arguments:
  path  Where to write; default <company-name>.yaml in the current directory.

Options:
  --force  Overwrite an existing file.
  --help   Show this message and exit.
```

### `poseidon blueprint preview`

```text
Usage: poseidon blueprint preview [OPTIONS] {blueprint}

  Show what applying BLUEPRINT would change — the diff against what the cluster
  hosts, and the budget. Changes nothing.

Arguments:
  blueprint  The blueprint file.  [required]

Options:
  --help  Show this message and exit.
```

### `poseidon blueprint apply`

```text
Usage: poseidon blueprint apply [OPTIONS] {blueprint}

  Apply BLUEPRINT: shows the diff, asks, applies through the console, then waits
  until the company is converged — live, its policy served — and says how long
  that took. Prints what was created and updated.

Arguments:
  blueprint  The blueprint file.  [required]

Options:
  -y, --yes             Apply without asking.
  --no-wait             Return as soon as the console has accepted the request.
  --wait-timeout <int>  Seconds to wait for the company to converge.  [default:
                        600]
  --help                Show this message and exit.
```

### `poseidon company`

```text
Usage: poseidon company [OPTIONS] COMMAND [ARGS]...

  The company this cluster hosts: show it, or delete it.

Options:
  --help  Show this message and exit.

Commands:
  show    The applied company: its name, phase (`activating` until its...
  delete  Delete the company: cancels its running runs, removes it and...
```

### `poseidon company show`

```text
Usage: poseidon company show [OPTIONS]

  The applied company: its name, phase (`activating` until its policy is served,
  `converged` after), generation and bundle revision, and who holds its
  heartbeat paused, if anyone.

Options:
  --json  Print exactly what the console answered, as JSON.
  --help  Show this message and exit.
```

### `poseidon company delete`

```text
Usage: poseidon company delete [OPTIONS]

  Delete the company: cancels its running runs, removes it and waits for the
  teardown. Asks first, naming the runs it will cancel. The one way to stop a
  company (`pause` holds it instead); applying its blueprint again brings it
  back.

Options:
  --reason <str>  Why. Recorded with the act, under your name.  [required]
  -y, --yes       Delete without asking.
  --help          Show this message and exit.
```

### `poseidon heartbeat`

```text
Usage: poseidon heartbeat [OPTIONS] COMMAND [ARGS]...

  The company's standing loop: show it, or wake it. `poseidon pause` holds it.

Options:
  --help  Show this message and exit.

Commands:
  show  The standing loop: its execution, and every tick — what it saw and...
  wake  Ask the loop to look now instead of at its next check-in.
```

### `poseidon heartbeat show`

```text
Usage: poseidon heartbeat show [OPTIONS]

  The standing loop: its execution, and every tick — what it saw and what it
  started, or why it did nothing.

Options:
  --since <str>  Only ticks at or after this timestamp.
  --json         Print exactly what the console answered, as JSON.
  --help         Show this message and exit.
```

### `poseidon heartbeat wake`

```text
Usage: poseidon heartbeat wake [OPTIONS]

  Ask the loop to look now instead of at its next check-in. It decides for
  itself what, if anything, to start.

Options:
  --help  Show this message and exit.
```

### `poseidon run`

```text
Usage: poseidon run [OPTIONS] COMMAND [ARGS]...

  One run: show it, read its story, cancel it.

Options:
  --help  Show this message and exit.

Commands:
  show    One run: its record — task, status, iterations, what it delivered...
  story   The run's story: every audit row, from every source, in order —...
  cancel  Cancel a run.
```

### `poseidon run show`

```text
Usage: poseidon run show [OPTIONS] {run_id}

  One run: its record — task, status, iterations, what it delivered — and its
  workflow's execution.

Arguments:
  run_id  The run's id.  [required]

Options:
  --json  Print exactly what the console answered, as JSON.
  --help  Show this message and exit.
```

### `poseidon run story`

```text
Usage: poseidon run story [OPTIONS] {run_id}

  The run's story: every audit row, from every source, in order — each tool
  call, decision, gate and verdict, with the actor's rationale where one was
  stated.

Arguments:
  run_id  The run's id.  [required]

Options:
  --decisions  Only the rows that carry a rationale: what was decided, and why.
  --json       Print exactly what the console answered, as JSON.
  --help       Show this message and exit.
```

### `poseidon run cancel`

```text
Usage: poseidon run cancel [OPTIONS] {run_id}

  Cancel a run. The console records the act and your reason under your name,
  then stops the workflow; the run's sandbox and agents are torn down.

Arguments:
  run_id  The run's id.  [required]

Options:
  --reason <str>  Why. Recorded with the act, under your name.  [required]
  --help          Show this message and exit.
```

### `poseidon start`

```text
Usage: poseidon start [OPTIONS] COMMAND [ARGS]...

  Start a run by hand, on a ticket or a pull request. An operator's tool: the
  heartbeat starts the company's runs on its own.

Options:
  --help  Show this message and exit.

Commands:
  implement  Start an implement run on a ticket and follow it to its end,...
  review     Start a review run on a pull request and follow it to its end.
```

### `poseidon start implement`

```text
Usage: poseidon start implement [OPTIONS] {ticket}

  Start an implement run on a ticket and follow it to its end, printing its rows
  as they land and then its record; exits non-zero if the run fails.

Arguments:
  ticket  The ticket's number on the repository.  [required]

Options:
  --no-wait  Return as soon as the console has accepted the request.
  --json     Print exactly what the console answered, as JSON.
  --help     Show this message and exit.
```

### `poseidon start review`

```text
Usage: poseidon start review [OPTIONS] {pr}

  Start a review run on a pull request and follow it to its end. The run parks
  on its merge gate until `poseidon verdict <run_id> merge approve|reject`
  answers, or policy merges on its own.

Arguments:
  pr  The pull request's number on the repository.  [required]

Options:
  --no-wait  Return as soon as the console has accepted the request.
  --json     Print exactly what the console answered, as JSON.
  --help     Show this message and exit.
```
