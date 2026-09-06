# Temporal — durable execution

A long-running agent process that dies takes its state with it. Retrying is
not obviously safe, because you do not know what already happened.

[Temporal](https://temporal.io/) inverts this. You write a *workflow* as
ordinary code, but the engine records every step and its result. If the
worker process dies, the workflow is replayed on a new worker from the
recorded history and continues from where it was. Side effects live in
*activities*, which are retried with policies you configure.

For Poseidon this buys two things that matter more than convenience:

- **Guaranteed compensation.** Teardown — revoking identities, destroying
  sandboxes, retiring registrations — runs on success, failure, cancel,
  timeout *and* worker death. There is one anchor for cleanup, and it always
  runs.
- **Human gates that survive restarts.** The merge gate is a workflow
  parked on a signal, possibly for hours. That is durable state, not a
  process holding a socket open and hoping. A person answers it through
  the console, and the workflow releases on the verdict the console
  recorded under their name — the signal only tells it to look.

The kind test suite kills the worker mid-run and asserts the run completes
with nothing leaked. The workflows, their signals and the surface beneath
the console are written down in [the Temporal contract](../reference/temporal.md).
