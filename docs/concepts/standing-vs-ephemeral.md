# Standing vs. ephemeral agents

Poseidon has two kinds of agent, and the difference is about how much
damage each could do.

| | Standing agent | Ephemeral agent |
|---|---|---|
| Declared | under `standing:` in the blueprint | as a role in a crew |
| Lives | as long as the company | for one run |
| Identity | stable, tied to a Deployment | created for the run, revoked at teardown |
| Example | the PM | the implementer, reviewer, steward, contract author |
| May write code | never | yes, in its own sandbox |

## Execution is always ephemeral

The compiler requires every standing agent to be a *conductor*: an agent
that may start crews (`may_spawn`). A blueprint that hires a standing
`implementer` doesn't compile. An agent that holds write tools and lives
forever is exactly the agent whose damage you can't bound.

## Starting a crew is not sharing tools

An ephemeral agent's tools don't have to be a subset of its conductor's.
The PM can't open a pull request, yet the crew it starts can.

Starting a crew means creating new agents from a fixed role the compiler
has vetted. Each agent gets the tools of its own role. The PM doesn't
lend its own authority. If every crew's tools had to be a subset of the
PM's, the PM would need the union of every tool it might ever hand out,
which is the opposite of least privilege.

The blueprint bounds what the PM can start: `may_spawn` names crews, not
tools, and `max_concurrent` caps how many run at once. Policy checks both
against a live count each time.
[Roles and crews](../reference/roles.md) lists the roles and what each may
start.
