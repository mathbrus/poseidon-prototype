# Poseidon

**An autonomous company, run by cloud agents, that cannot
exceed its authority — because the infrastructure will not let it, not
because the prompt asked nicely.**

You give Poseidon a charter — a vision to pursue, a budget to stay under,
a short list of things that must never happen without you. It stands up a
small software organisation to pursue that vision: a steward that writes
the strategy and the architecture masterplan from it, a product manager
that scopes the next slice and files tickets, ephemeral crews that
implement and review each change, and a standing heartbeat that decides
what to do next and
keeps going. Nothing else goes in — no goal file, no task, no "please do
X". Real pull requests appear on your repository. You approve the merges
that matter and watch the rest happen. When you are ready to grant more, the
company does more of its own SDLC unattended — up to and including merging
its own passing work — and never one step past the authority you wrote down.

The distinction in that first sentence is the whole point. Plenty of tools
let an agent open a pull request. What is hard, and what Poseidon is about,
is letting a *company of agents work toward a vision on its own* while
guaranteeing it cannot force-push to `main`, spend past its budget,
exfiltrate a secret, or act on an instruction hidden in a GitHub issue — not
because a careful prompt asked it not to, but because the surrounding system
makes those outcomes impossible and records every attempt.

## The company is the product

Most agent frameworks are factories: hire an agent for one task, get an
output, tear it down. Poseidon is trying to be a *company* — a control loop
with memory that observes the world, decides what is worth doing, does it,
learns, and repeats, for as long as the vision stands. That ambition sets
the bar in five places:

- **Autonomy** — the system chooses the next action from its documents and
  its state; a human declares the gates and the caps but does not stitch
  the phases together, and does not hand in work.
- **Continuity** — a strategy and an architecture masterplan the company
  wrote for itself, in its repository; standing agents that remember across
  restarts. Unfinished work survives a worker dying; durable execution is
  why.
- **Coordination** — many workstreams, shared artifacts, real priorities: a
  PM sequencing tickets, crews that implement and review, a heartbeat that
  picks what runs next.
- **Quality** — permission is the easy third of governance. The hard two are
  whether the work was worth doing and whether it is actually good — so the
  platform, never the agent, is the gate of record for "the tests pass," and
  the tests themselves can be split off where the implementer cannot rewrite
  them.
- **Authority** — every consequence is decided by policy a human can read,
  diff and revoke. Injection is assumed; containment is positional; spend is
  a gate, not a warning: an exhausted budget parks the run until a human
  answers.

This is a working prototype, and it is honest about the distance still to
travel. The destination is a company that lives in the cloud and actuates on
the real world — customers, mail, money — not just a repository. Today it
reaches code, under governance, on a local cluster. [What it cannot do](argument/cannot.md)
is deliberately specific about the gap.

!!! note "Status: working prototype"
    Every capability described here is exercised by the end-to-end suite
    against a real Kubernetes cluster, and the suite proves its own claims
    twice over — two consecutive green runs from a clean repo, ending with
    the cluster asserted quiet. It has run only on kind (local Kubernetes),
    never in production.

## How to read this site

[**The argument**](argument/problem.md) is written to be read start to finish. It teaches the
ideas first, then shows where each one lives in the code. If you have never
worked with MCP, OPA, or Kubernetes NetworkPolicy, [**Concepts**](concepts/index.md) is there to
teach you, not to name-drop.

If you would rather *use* it than understand it first, start with the
[**Guides**](guides/getting-started.md). When you need to look something up — a blueprint field, a console
route or a CLI flag, which role holds which tool — that is what [**Reference**](reference/blueprint.md) is for, and
those pages are generated from the code so they cannot drift from it.
