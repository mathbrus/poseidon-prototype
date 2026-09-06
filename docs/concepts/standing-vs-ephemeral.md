# Standing vs. ephemeral agents

Poseidon distinguishes two kinds of agent, and the distinction is
structural rather than cosmetic.

A **standing agent** is a hire: declared under `standing:` in the
blueprint with a role and a count, reconciled into a Deployment by the
operator, and holding a stable identity that outlives any run. In the
shipped example there is exactly one: the PM.

An **ephemeral agent** exists only inside a run. It is bound when a
crew is fulfilled, gets a run-scoped identity, and is revoked at teardown.
It never appears under `standing:` and leaves no standing structure behind.

The rule the compiler enforces is that **execution is always ephemeral**. A
standing agent must be a *spawner* — a conductor with `may_spawn` authority.
A blueprint that tries to hire a standing `implementer` is a compile
error, because a standing agent holding write tools is exactly the thing
whose blast radius you cannot bound.

## Spawn authority is not a capability ceiling

One consequence that surprises people: an ephemeral agent's tool scope is
**not** required to be a subset of its conductor's. The PM cannot open a
pull request, yet the crew it spawns can. Spawn authority is not a
capability ceiling — it is authority to *create a principal*, whose own
scope comes from its role in the crew. Keeping ⊆ would have forced the PM's
scope to become the union of everything it might ever spawn, which is the
opposite of least privilege.

Readers who know the capability literature will recognise the shape: the
conductor holds something like a *factory* — the right to mint a new
principal from a fixed, compiler-vetted template — rather than the right to
delegate its own authority. The blueprint bounds what the factory can
produce (`may_spawn` names crews, not tools), and compiled policy enforces
the bound at spawn time against a live concurrency count. The roles, the
crews and what each may spawn are on [Roles and crews](../reference/roles.md).

## Future: other kinds of agents

The one future hire the design already names is the **observer**: a
standing agent that reads the world outside the repository and would be
the author of strategy revisions — today the strategy is written once by
the steward and then changed by no current role, a compiled deny rather
than restraint, and neither the steward, the PM nor the `decide`
procedure grows to fill that gap. An observer, or a support or accounting
agent, would enter the same way as today's five roles: a catalog role with
its own tool ceiling and procedures, dispatched by a workflow, contained
by compiled policy rather than by its prompt. What would have to give way is
the gateway's fixed tool catalog (new tools, with their own run-scoping
rules) and the assumption that every run has a sandbox and a repository.
None of that exists in the tree today; it is a consideration for later,
recorded so the shape above is not mistaken for a limit.
