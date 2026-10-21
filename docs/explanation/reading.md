# Further reading

The design leans on ideas with long histories. If this site was your
introduction to them, these are the primary sources worth your time.

## Classical protection

- Saltzer & Schroeder, *The Protection of Information in Computer Systems*
  (Proc. IEEE, 1975) — the design principles [The core idea](how-it-works.md)
  instantiates: complete mediation, fail-safe defaults, least privilege,
  separation of privilege, economy of mechanism.
- Anderson, *Computer Security Technology Planning Study* (1972) — the
  reference-monitor concept: always invoked, tamper-resistant, small enough
  to inspect. The MCP and LLM gateways are an attempt at "always invoked"
  for agent effects.
- Hardy, *The Confused Deputy* (ACM SIGOPS, 1988) — why a
  credential-holding intermediary must bind its decisions to the verified
  requester, never to its own ambient authority.
- Miller, Yee & Shapiro, *Capability Myths Demolished* (2003) — the
  capability-discipline lens behind "spawn authority is not a capability
  ceiling."

## Prompt injection

- Greshake et al., *Not What You've Signed Up For: Compromising Real-World
  LLM-Integrated Applications with Indirect Prompt Injection* (2023) — the
  systematic case that any text reaching a model is an instruction channel.
- Willison, [*The Lethal Trifecta*](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
  (2025) — private data, untrusted content, and an exfiltration channel in
  one agent; Poseidon's egress design is an exercise in amputating the
  third leg.
- OWASP, [*Top 10 for LLM Applications*](https://owasp.org/www-project-top-10-for-large-language-model-applications/)
  — LLM01 is prompt injection; this repository is one long answer to "and
  what if you cannot prevent it?"

## The systems underneath

- [OPA documentation](https://www.openpolicyagent.org/docs/) on bundles and
  decision-making; [Temporal documentation](https://docs.temporal.io/) on
  workflow determinism and replay; Kubernetes documentation on
  [ServiceAccount token projection](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/)
  and [NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

## Questions to take to a seminar

If you are reading this with a class, these are the places where
the design genuinely has something to argue about.

1. The merge is released on a verdict the console recorded, not on the
   signal ([What it cannot do](limitations.md#the-merge-release-trusts-the-console-and-the-registry)).
   The artifact is an unsigned audit row. Name what it trusts that the
   signal did not, decide whether signing it would buy anything against
   the writers who could forge it, and say what a compromised console
   can and cannot do.
2. `bound_to_run` guards sandbox tools but not `git.open_pr`
   ([OPA and Rego](../concepts/opa.md)). Reconstruct the reasoning: what property of opening a pull
   request makes run binding the wrong guard, and which compiled rules
   stand in its place?
3. The platform trusts its own compiled policy bundle. How would you
   detect a compromised or buggy compiler — and does the golden-file
   workflow in [Contributing](../developing/contributing.md) count as detection, or only as review?
4. The PM can spawn crews holding tools the PM itself may not use
   ([Standing vs. ephemeral agents](../concepts/standing-vs-ephemeral.md)). Argue both sides: privilege-escalation hole, or the
   correct reading of least privilege?
5. [What it cannot do](limitations.md#unauthenticated-in-cluster-services-guarded-only-by-networkpolicy) concedes that in-cluster surfaces rely on NetworkPolicy
   alone, and that a port-forward passes beneath it. Rank the five
   unauthenticated surfaces by consequence-if-reached now that a signal
   to the frontend releases nothing, decide which you would authenticate
   first, and say whether the console's identity model — a ServiceAccount
   per person, a token for one audience — would serve it.
