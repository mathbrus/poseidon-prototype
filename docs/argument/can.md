# What it can do

This is the product, stated as capabilities rather than architecture — and
every line is asserted by the end-to-end acceptance suite, not a roadmap.
Read it as the answer to "what do I actually get?"

- **A software company from one document.** Compile, preview, apply, edit and
  tear down a whole company from a single blueprint; hiring and firing are
  diffs you read before they happen; firing revokes an agent's identity the
  moment its row is deleted.
- **A company from a vision alone.** The human writes the vision; the
  company writes its own strategy and architecture masterplan from it
  (the steward, through pull requests like any other change), scopes the
  next slice into tickets from those documents, and never accepts a goal
  from anyone. The strategy is written once; the masterplan changes only
  through an architecture request a crew records and the platform files.
- **A full SDLC.** Strategy → tickets (scope) → implementation by an
  ephemeral implementer crew → platform-run verification → pull request →
  ephemeral review with a posted findings comment and a structured
  verdict → merge.
- **Quality as a gate policy owns, not the agent.** The merge gate resolves
  the way the blueprint declares. Delegate it, and a change in the declared
  path class merges only when the independent review passed *and* the
  platform's own re-run of the tests is green *and* the reviewer's identity
  is not the implementer's — three facts, no LLM's word taken for any of
  them; the merge fires with no human command. Keep the gate human, and
  an approval is a verdict recorded under a verified name — the console's
  audit row — that policy, not the signal, releases on. A failed layer files a
  bounded rework ticket the heartbeat consumes, and at the rework bound it
  escalates to the human; a change touching a protected surface (a merged PR
  must not be able to rewrite the rules that govern merging) is refused by
  policy and falls back to the human signal. Removing the delegation reverts
  to human-only merge — revocation is a diff.
- **The oracle the implementer cannot rewrite.** Delegating on "the tests
  pass" only works if the workstream that writes the code cannot also write
  the tests that grade it. A blueprint can split off an acceptance-contract
  tree that a *different* crew authors and only a human merges; the gating
  fact then becomes that contract *as it stands on the base ref*, run
  against the new code, so a suite weakened to green launders nothing. The
  implementer cannot certify its own work — the containment is
  positional, tested by a red-team that tries every way around it.
- **Unattended operation under caps.** Every company runs a standing
  heartbeat that runs the backlog itself: it dispatches the PM to choose
  the next action, starts it under compiled policy and declared caps
  (`max_concurrent_runs`, `max_open_tickets`, a daily spend cap),
  journals every decision, and halts visibly at the cap. With a human
  merge gate that is one approval out and nothing in — the audited act
  of a named person, through the console; with a delegated
  gate the company merges its own in-class passing work too, so the arc
  has no human act at all. A poisoned ticket or document cannot start
  work outside the observation, and deleting the company stops the loop
  at the next tick.
- **Durability.** Kill the orchestrator worker mid-run; Temporal replays it
  and the run completes, with no leaked sandbox, identity row
  or registration.
- **Governance that holds under attack.** Compiled-policy denies for
  out-of-scope tools; an injected conductor refused when it tries to spawn
  outside `may_spawn`; egress containment with the attempt audited; six
  distinct audit sources correlated by one `run_id`.
- **Budget enforcement.** A per-run hard stop that pauses the run visibly,
  escalates as a real gate event, and attributes every dollar to
  `{run, parent, agent, role}`.
- **Sandbox containment.** No mounted service-account token, no reachable
  cluster API or node metadata, no runtime socket or host mount, no foreign
  processes, unforgeable per-run egress identity, resource caps that
  actually terminate a runaway, and a wall-clock deadline on every job.
