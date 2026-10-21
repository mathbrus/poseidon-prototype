# Workflows and signals

Every act through the [console](console.md) ends as a call to Temporal: a
workflow started, a signal delivered, an execution read. This page lists
the workflows the orchestrator registers, their signals and their
queries. The tables are rendered from the workflow classes.

All workflows run on the task queue `poseidon-orchestrator`. Workflow ids
are fixed: `{workflow}-{run_id}` for a run, `heartbeat-{company}` for the
heartbeat. Both formats live in `common.workflows`.

## Workflows

<!-- gen: workflows -->

- `implement` takes the number of a ticket the company filed. The platform
  reads the issue and composes the task from it.
- `scope` takes only a run id. Its input is the observation the platform
  composes.
- The orchestrator worker starts `heartbeat` for the applied company and
  keeps it running. It stops when the company is deleted.

Activity timeouts are not part of any input. The worker's
`TIMEOUT_PROFILE` (`default` for a paid model, `fast` for the mock) sizes
every workflow, and each workflow reads it in its first activity.

## Signals

A signal tells a parked workflow to look. It is never the verdict itself:
the console records the verdict first, and the merge reads that record
([The merge gate](../explanation/merge-gate.md)). `wake` carries nothing.
It ends the heartbeat's sleep, and the tick it causes is decided like any
other. A pause is not a signal: the console records it in the registry,
and the heartbeat reads it on every tick.

<!-- gen: signals -->

## Queries

Every workflow answers a `state` query with its current state as a
string, such as `parked-merge_gate`. The console shows it when you read a
run, and requires the parked state before it records a verdict.

## What guards this surface

The Temporal frontend has no authentication of its own. NetworkPolicy
admits only the orchestrator worker, the console and Temporal's own pods.
A person with cluster credentials can still reach it through
`kubectl port-forward`, but a merge signal sent that way finds no
recorded verdict and releases nothing. `tests/e2e_kind/test_console_redteam.py`
checks this on every acceptance run.
[Limitations](../explanation/limitations.md#some-services-rely-on-networkpolicy-alone)
covers what remains open.
