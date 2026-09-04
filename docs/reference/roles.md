# Roles and crews

A **role** is what an agent *is*: its tool ceiling, the crews it may
spawn, and the procedures a dispatch may ask of it. A **crew** is the list
of roles fulfilled together for one run; each becomes one ephemeral agent
with its own pod and identity, under the conductor's authority. Both
catalogs are closed: a blueprint names roles and crews, it never defines
them, because a role a blueprint could invent would be a policy the
platform never reviewed.

The tables on this page are rendered from `packages/schema/src/schema/catalog.py`
at build time.

## Roles

<!-- gen: roles -->

A blueprint may **narrow** a role's tool list in its `tools.<role>` section,
never widen it, and the scope applies to every agent of the role, standing
or ephemeral. The lean-standing rule is not in the table because it is the
compiler's: `implementer`, `reviewer` and `contract-author` exist only as
ephemeral agents, and a blueprint that hires one standing is a compile error.

## Which role runs which procedure

A **procedure** is the agent host's unit of work. A dispatch names one; the
host refuses any the role does not declare before a model call is made.

<!-- gen: procedures -->

## Crews

<!-- gen: crews -->

A blueprint names the crews it puts to work in a hire's `may_spawn` and a
workflow's `crew`, never their members. Spawn authority is authority to
create a principal from a compiler-vetted role, not a capability ceiling:
the PM cannot open a pull request, yet the crew it spawns can.
