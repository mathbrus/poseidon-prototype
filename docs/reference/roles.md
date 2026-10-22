# Roles and crews

A **role** defines what an agent is: the tools it may hold, the crews it
may start, and the procedures it may run. A **crew** is a list of roles
that work together on one run. Each role in a crew becomes one ephemeral
agent, with its own pod and identity.

Both catalogs are fixed in code. A blueprint names roles and crews, but
never defines them, so no blueprint can invent a permission nobody
reviewed. The tables are rendered from
`packages/blueprint/src/blueprint/catalog.py`.

## Roles

<!-- gen: roles -->

A blueprint may narrow a role's tools in its `tools` section, never widen
them. Only `pm` may be a standing agent. The other roles exist only
inside a run, and the compiler refuses a blueprint that hires one as
standing.

## Which role runs which procedure

A **procedure** is one unit of work an agent runs. The orchestrator names
one in each dispatch, and the agent host refuses a procedure its role
doesn't declare, before any model call.

<!-- gen: procedures -->

## Crews

<!-- gen: crews -->

A blueprint puts crews to work in a standing agent's `may_spawn` and a
workflow's `crew`. A crew's roles may hold tools its conductor doesn't:
the PM can't open a pull request, but the crew it starts can
([why](../concepts/standing-vs-ephemeral.md#starting-a-crew-is-not-sharing-tools)).
