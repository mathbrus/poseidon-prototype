# How the heartbeat decides

Every company has a heartbeat: one long-running workflow that decides what
the company does next. Nobody starts the company's runs by hand. The
heartbeat starts them, within the limits your blueprint sets. A blueprint
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

## The limits it must stay within

| Limit | The heartbeat refuses to start a run when… |
|---|---|
| `may_start` | the workflow is not on the list. A company that may not `implement` waits for you to run `poseidon start implement <n>`. |
| `max_concurrent_runs` | that many of the company's runs are already in flight. |
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
implement run delivered a pull request, or a review reached a verdict.
That holds whatever the gate did next.

A run that ends without concluding anything, because a model call timed
out or the agent hit its step limit, leaves its item for another try. The
item is offered again until `max_attempts` runs have tried. The run that
spends the last attempt records a `gate_exhausted` event and comments on
the ticket or pull request, so the company never silently gives up.

A bootstrap ticket whose every attempt fails halts the loop with
`heartbeat_bootstrap_exhausted` instead of planning without a strategy.
Close the ticket and the heartbeat files a new one.

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
