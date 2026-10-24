# How the heartbeat decides

Every company has a heartbeat: one long-running workflow that decides what
the company does next. Nobody starts the company's runs by hand. The
heartbeat starts them, within the caps your blueprint sets. A blueprint
without a `heartbeat` section doesn't compile.

```yaml
heartbeat:
  conductor: pm
  reconcile_minutes: 30
  max_concurrent_runs: 1
  max_attempts: 2
  max_open_tickets: 5
  may_start: [scope, implement, review]
  daily_cap:
    usd: 20
```

## When it ticks

The heartbeat sleeps between *ticks*. It wakes up:

- whenever one of the company's runs ends,
- when you run `poseidon heartbeat wake`,
- at the latest every `reconcile_minutes`, so it notices changes made
  outside the company, such as an issue you filed or a pull request you
  merged by hand.

A wake-up carries no instruction. Every tick observes, decides and is
authorized the same way.

## What happens in a tick

1. **Observe.** The heartbeat reads the strategy and the masterplan from
   the base branch, the open tickets, the delivered pull requests still
   open on GitHub, the runs in flight, and the day's spend.
2. **Bootstrap.** If a document is missing, it files a bootstrap ticket
   for it, once, under the platform's identity.
3. **Decide.** It asks the conductor, the PM, to choose exactly one
   action: `scope`, `implement #n`, `review #n`, or `idle`, with a
   one-line reason. The decision is recorded as a `heartbeat.decide` audit
   event.
4. **Authorize.** The platform starts the chosen run only if it passes the
   compiled `heartbeat.start` decision.

The PM proposes and the platform disposes. The PM's choices are a closed
set, and its target must be a ticket or pull request the tick observed.
A poisoned ticket title or document can at most steer the choice between
real candidates. It can't create a new one.

The PM sees only ticket numbers, titles and URLs, never ticket bodies. The
documents reach it after those lists, clipped and marked untrusted.

## What it must stay within

| Field | The heartbeat refuses to start a run when… |
|---|---|
| `may_start` | the workflow is not on the list. A company that may not `implement` waits for you to run `poseidon start implement <n>`. |
| `max_concurrent_runs` | that many of the company's runs are already in flight. A review waiting on your merge verdict doesn't count, unless its pull request edits the contract tree. |
| `daily_cap.usd` | the day's spend has reached the cap. |
| `max_attempts` | that many runs have already taken up the ticket or pull request. |
| `max_open_tickets` | (for `scope` only) that many tickets are already open. |
| both documents | (for `scope` only) the strategy or the masterplan is still missing. |
| a pause | you hold the company with `poseidon pause`. |

If a count is missing from the decision's input, the start is refused. A
missing fact never counts as permission.

Every field of the section is required. A cap with a default value would
be authority nobody wrote down.

## Retries and attempts

A ticket or pull request leaves the backlog when a run *concludes* it: an
implement run delivered a pull request that was not refused, or a review
reached a verdict.

A run that ends without concluding anything, because a model call timed
out or the agent hit its step limit, leaves its item for another try. The
item is offered again until `max_attempts` runs have tried.

## How a ticket ends

Every ticket ends one of four ways, each recorded and shown on GitHub:

| Ending | What happens |
|---|---|
| Its pull request merges | The platform closes the ticket as completed. A rework chain's tickets close with it. |
| Its pull request is refused | The pull request closes without merging, with the reason on it: your rejection, an expired gate, a head or base that moved, a review that never reached a verdict, or a close on GitHub. The ticket comes back as one spent attempt, says why, and the next attempt reads the reason. |
| Its attempts are spent | The ticket is abandoned: closed as not planned with its rework chain, the notice on it. |
| The PM dismisses it | While it scopes, the PM may close a ticket the company should not work, as not planned, saying why. |

A bootstrap ticket is never closed for spent attempts. The loop halts on
it with `heartbeat_bootstrap_exhausted` instead of planning without a
strategy. Close the ticket and the heartbeat files a new one.

The open tickets are the backlog the company works on, plus issues
others filed that the PM has not yet read. `max_open_tickets` bounds
them.

## What the PM remembers

Nothing private. The PM's state across ticks is the registry and the
documents, read fresh on every tick. The record of why the company did
what it did is the `heartbeat.decide` events. Read them with:

```bash
uv run poseidon run story heartbeat-<company> --decisions
```

## Pausing and stopping

`poseidon pause` holds the heartbeat: a paused tick records `tick_paused`
with your name and starts nothing. Runs in flight finish. Deleting the
company is the only way to stop the heartbeat.
[Pause, resume or delete](../how-to/pause-resume-delete.md) has the
commands.
