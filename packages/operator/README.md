# operator

Reconciles the two custom resources — `Company` and `AgentInstance` — into
a live company: a namespace, the NetworkPolicy envelope, the
orchestrator's bounded sandbox-provisioner RBAC, standing agent
Deployments, and compiled policy bundles persisted for OPA to poll.

The importable name is `poseidon_operator` because `operator` is a Python
standard-library module.

This package is the second half of a deliberate split: the
[compiler](../compiler/README.md) is a pure function that decides what
enforcement should exist and touches no cluster; the operator makes it
exist. Apply is the **only** mutation of enforcement, and this package is
the only thing that applies — so "why is this NetworkPolicy here?" always
traces to a blueprint line, through the compiler, through here, and
nowhere else.

## What "reconcile" means here

An *operator* in the Kubernetes sense is a controller for custom
resources: something writes a `Company` CR to the API server, and this
process notices and makes the cluster match it. The important design
choice is *how*. The naive shape is edge-triggered: react to each event
with the action it implies ("a Company appeared → create a namespace").
That is brittle, because correctness then depends on having seen every
event exactly once — a crashed operator misses an edge and stays wrong
forever.

The reconcilers in `reconcile.py` are written level-triggered instead: on
*any* event for a CR (create, update, or `resume`, kopf's replay of every
existing CR at operator startup), the handler recomputes the entire
desired state from the spec and upserts all of it, whole. Nothing is
computed from the event's diff. The payoff is that correctness depends
only on the *current* spec, never on event history: a restart, a resync or
a repeated apply converges to the same state rather than accumulating, and
a missed event is repaired by the next one.
`test_company_reconcile_is_idempotent` pins exactly this — reconciling
twice records the same intents twice, no drift.

Teardown follows the same philosophy: `teardown_company` sweeps registry
rows at the company level (`AgentInstances.delete_for_company`) rather
than trusting that an instance-delete event was seen for each standing agent.

One consequence worth naming: the `phase: converged` status means *the
operator's own work is done* — structure applied, bundle persisted. OPA
activates the bundle on its own next poll (below), a beat later.

## Hard boundaries

The operator owns **standing structure and nothing else**. It never
creates ephemeral agents — those exist only inside a run and belong to the
orchestrator's fulfill activities — and it never runs a process. The
reason is blast radius: standing structure changes only when a human
applies a blueprint, so its mutator can be slow, auditable and rare; runs
happen constantly and are driven by LLM output, so the thing acting inside
them (the orchestrator) gets a separately bounded, separately reviewed
authority. The operator's ClusterRole makes the boundary mechanical, not
aspirational: it grants the CRs, namespaces, networkpolicies,
roles/rolebindings, deployments, services and serviceaccounts — and **no
pods, no exec, no jobs, no secrets**. An operator bug (or compromise)
cannot execute anything.

## Module map

| Module | Owns |
|---|---|
| `reconcile.py` | The kopf handlers: create/update/resume/delete for both CRs, plus the startup hook that moves kopf's bookkeeping into annotations. |
| `resources.py` | Every Kubernetes object the operator owns, as **pure builders** — what the operator *would* apply is testable without a cluster. |
| `kube.py` | `Cluster`: the intent-level client (`upsert_namespace`, `upsert_deployment`, …), one seam the reconciler tests replace with a fake. |
| `bundleserver.py` | The OPA bundle server, a separate entry point sharing the package. |
| `config.py` | `OperatorSettings`: the platform namespace, the agent image and run configuration the builders take as arguments. |

## What one Company converges to

`reconcile_company` upserts, in order:

- **The namespace**, labelled `poseidon.dev/company: <name>`.
- **Four NetworkPolicies.** `default-deny` covers every pod both ways and
  is the envelope every other rule punches through — fail-safe defaults as
  infrastructure. `agent-egress` lets agent pods reach exactly the two
  gateways (MCP on 8080, LLM on 4000) plus cluster DNS; `agent-ingress`
  admits only the orchestrator worker. Both select pods by
  `poseidon.dev/kind` in `{standing-agent, ephemeral-agent}`, so an ephemeral
  ephemeral pod is contained by a policy that existed before the pod did.
  `sandbox-isolation` is projected from the compiled infra IR carried on
  the CR spec (`spec.infra.network_policy`) — hand-authoring that
  projection here would be a second source of truth, i.e. a bug. It
  grants **no cluster DNS**: the sandbox needs no resolver (the egress
  proxy resolves target hosts), and a resolver forwarding unknown names
  upstream is an exfiltration channel the audited proxy cannot see.
- **The sandbox-provisioner Role and RoleBinding** — the orchestrator
  worker's bounded surface in this namespace (Jobs, pods, `pods/exec`;
  never Deployments, Services or CRs). See the escalate/bind section.
- **The policy bundles**, persisted to `registry.policy_bundles`: the
  company bundle under the company's name, the platform bundle under the
  reserved key `"platform"` (the compiler refuses it as a company name).
  Revisions are content-addressed and the compiler emits the platform
  bundle identically from every blueprint, so re-applies never churn it.
- **The registry row** (`companies`), the applied spec minus the bundle —
  what the orchestrator and gateways read at runtime.

## What one AgentInstance converges to

`reconcile_instance` upserts the **ServiceAccount first** — named by the
`agentId`, it is the identity the Deployment runs as — then the
Deployment (named by the `standingName`) and a Service, and records the agent
in `registry.agent_instances` with its namespace. The gateway resolves
`system:serviceaccount:<ns>:<agent_id>` against that row and requires
the namespace to match, so a same-named ServiceAccount anywhere else
verifies fine at the API server and still maps to nothing.

The Deployment is credential-free by construction:

```python
"serviceAccountName": spec["agentId"],
# The projected volume below is the pod's only token:
# audience-scoped to the gateway, useless at the API
# server. Automount stays off.
"automountServiceAccountToken": False,
```

The pod's only identity is a kubelet-rotated projected token scoped to
the audience `poseidon-mcp-gateway`; no API-server token is mounted, no
`secretKeyRef` appears anywhere in the env, and `GPG_KEY` (baked into the
python base image) is blanked so no token-shaped value exists in a
company pod at all. Everything the agent reads about itself and its
company (role, repository, vision) rides the pod *template*, which is
what makes a blueprint edit converge as a rollout rather than requiring a
manual restart.

**Firing is the delete handler.** Deleting an `AgentInstance` deletes its
Deployment, Service and ServiceAccount — and the ServiceAccount deletion
is what revokes the identity: the API server refuses TokenReview for
tokens of an SA that no longer exists, so a fired agent's projected token
dies with it (bounded by the gateway's authn cache TTL). Revocation is a
consequence of structure, not a separate cleanup step someone must
remember.

## The two entry points

Both run the `poseidon-operator` image, differing only in command:

```
kopf run -m poseidon_operator.reconcile --all-namespaces   # the reconciler
python -m poseidon_operator.bundleserver                   # the bundle server
```

The bundle server builds its FastAPI app in `main()` rather than at
import, and connects to the registry lazily on the first bundle request:
on a fresh cluster it can start before Postgres accepts connections, so
an eager connect would crash-loop it. `/healthz` stays database-free.

## Bundles are persisted and polled, never pushed

**Nothing is ever pushed into OPA.** The operator persists a compiled
bundle to `registry.policy_bundles`; the bundle server hands it to OPA
over the standard bundle API at `/bundles/{company}/bundle.tar.gz`; OPA
polls. Three properties fall out of this direction of flow:

- **Restart safety.** A restarted OPA re-fetches the current revision
  instead of running on whatever it last held in memory: a push design
  needs someone to notice the restart and re-push; polling makes a fresh
  start indistinguishable from any other poll.
- **Apply stays the single mutation point.** No second code path can
  install policy into a running OPA, so "what is OPA enforcing?" always
  has the same answer as "what was last applied?".
- **Teardown revokes.** `teardown_company` deletes the stored bundle, and
  the server answers 404 for a company that was never applied or has been
  torn down — the honest answer, and the mechanism by which a caller still
  holding an identity finds no rule left to match. The platform bundle
  survives: it belongs to the platform, not to any company.

Standard bundle semantics keep this cheap: `ETag` carries the
content-addressed revision and a matching `If-None-Match` gets a 304, so a
steady state costs one small request per poll interval, not a tarball.

## The escalate/bind grant

The operator's ClusterRole holds `escalate` and `bind` on roles and
rolebindings. Kubernetes normally refuses to let a principal grant
permissions it does not itself hold — the right guard against privilege
escalation, and exactly what must be selectively waived here: company
namespaces are operator-created, so the orchestrator's sandbox-provisioner
Role *inside* them must be operator-created too, and that Role grants
jobs/pods/exec, which the operator deliberately does not hold. The grant
is scoped to roles/rolebindings only, so the ownership matrix stays
intact — the operator hands the worker its bounded surface while still
being unable to touch a pod itself. The alternative — widening the
operator's own RBAC to cover pods and jobs — would be strictly worse.

## Two traps the code had to design around

**kopf's bookkeeping lives in annotations.** The CRDs declare structural
status schemas (only `phase`/`bundleRevision`/`message` survive pruning),
so kopf's default status-based progress storage would be silently dropped
by the API server. The startup hook moves progress and diffbase storage
into `poseidon.dev`-prefixed annotations, and handlers write status via
`patch.status` explicitly rather than through return values (which land
under `status.<handler_id>` — pruned).

**NetworkPolicies are replaced, never merge-patched.** Every other upsert
in `kube.Cluster` is create-or-patch with the whole body, which is enough
for convergence. Policies get a stricter treatment:

```python
def upsert_network_policy(self, namespace: str, body: dict[str, Any]) -> None:
    """Replace, never merge-patch: a policy's selector shape can change
    (matchLabels to matchExpressions, say), and a merge would
    keep both — an impossible AND that silently deselects every pod
    the policy was meant to cover."""
```

A deselected policy fails open — the pods it was meant to confine become
unconfined, silently. This is the kind of failure mode worth a dedicated
code path.

## Cross-package interactions

- Consumes the `compiler` output carried on the `Company` CR spec
  (`policyBundle`, `platformBundle`, `infra`, `workflows`, `budgets`).
- Writes `companies`, `agent_instances` and `policy_bundles` through
  `registry`; those rows are what the orchestrator, the gateways and the
  egress proxy read at runtime.
- Builds agent pod specs from `common.podsecurity` (non-root uid 10001,
  no escalation path, all capabilities dropped).
- The CRD definitions themselves live in `crds/` at the repo root; the
  operator's ClusterRole and both Deployments are in
  `deploy/helm/poseidon/templates/operator.yaml`.

## Tests

```
uv run pytest packages/operator
```

`test_resources.py` asserts on the built objects directly — no cluster
needed, which is the payoff of keeping the builders pure.
`test_reconcile.py` drives the handlers against a fake `Cluster` and fake
registry accessors, pinning *what the operator does*: which objects it
converges, what it sweeps at teardown, what status it writes.
`test_bundleserver.py` exercises the bundle API against an in-memory
store. The live behaviour (namespaces really created, NetworkPolicies
really enforced, a fired agent's token really rejected) is proven by
`tests/e2e_kind/test_operator_lifecycle.py`.

## Invariants

- Standing structure only; ephemeral agents are never the operator's, and
  its RBAC cannot touch a pod, an exec, a job or a secret.
- Reconciliation is idempotent: every object is sent whole and upserted,
  so repeated applies converge instead of accumulating.
- Deleting an `AgentInstance` deletes its ServiceAccount, and that is
  what revokes the identity — the API server refuses TokenReview for
  tokens of an SA that no longer exists.
- Agent pods carry no credential env at all — not even a `secretKeyRef`.
  The pod's only identity is an audience-scoped projected token, and
  `GPG_KEY` is blanked so no token-shaped value exists in a company pod.
- The sandbox NetworkPolicy grants **no cluster DNS**. The sandbox needs
  no resolver, and one that forwards unknown names upstream would be an
  exfiltration channel the audited proxy cannot see.
- Policy reaches OPA by poll, never by push; a torn-down company's bundle
  stops being served, and the platform bundle outlives every company.
