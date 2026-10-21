# Temporal

An agent process that crashes takes its state with it, and retrying isn't
safe if you don't know what already happened.
[Temporal](https://temporal.io/) solves this with *durable execution*.
You write a *workflow* as ordinary code, and the engine records every
step and its result. If the worker dies, another worker replays the
workflow from the record and continues where it stopped. Side effects
live in *activities*, which are retried by policies you set.

## Why Poseidon uses it

- **Cleanup always runs.** Revoking identities and deleting sandboxes
  happens on success, failure, cancellation, timeout and worker death.
- **Gates survive restarts.** A run waiting for your merge verdict may
  wait for hours. It's durable state, not a process holding a socket open.

The acceptance suite kills the worker mid-run and checks that the run
completes with nothing leaked.
[Workflows and signals](../reference/workflows.md) lists the workflows
Poseidon registers.
