# operator

Reconciles the two custom resources — `Company` and `AgentInstance` — into a
live company: a namespace, the NetworkPolicy envelope, the orchestrator's
bounded sandbox-provisioner RBAC, standing agent Deployments, and compiled
policy bundles persisted for OPA to poll.

The importable name is `poseidon_operator` because `operator` is a Python
standard-library module.

Apply is the **only** mutation of enforcement, and this package is the only
thing that applies. That is the other half of the compiler's purity: one
pure function decides, one reconciler acts.

## Hard boundaries

The operator owns **standing structure and nothing else**. It never creates
ephemeral agents — those exist only inside a run and belong to the
orchestrator's fulfill activities — and it never runs a process. Its RBAC
stops exactly there: no pods, no exec, no jobs, no secrets.

Reconciliation is idempotent by construction. Every object is sent whole and
upserted, so a restart, a resync or a repeated apply converges to the same
state rather than accumulating.

## Module map

| Module | Owns |
|---|---|
| `reconcile.py` | The kopf handlers: create/update/resume/delete for both CRs. |
| `resources.py` | Every Kubernetes object the operator owns, as **pure builders** — what the operator *would* apply is testable without a cluster. |
| `kube.py` | `Cluster`: the intent-level client (`upsert_namespace`, `upsert_deployment`, …), so the reconcilers stay thin enough to read. |
| `bundleserver.py` | The OPA bundle server, a separate entry point sharing the package. |

## The two entry points

Both run from `Dockerfile.operator`, differing only in command:

```
kopf run -m poseidon_operator.reconcile --all-namespaces   # the reconciler
python -m poseidon_operator.bundleserver                   # the bundle server
```

**Nothing is ever pushed into OPA.** The operator persists a compiled bundle
to `registry.policy_bundles`; the bundle server hands it to OPA over the
standard bundle API; OPA polls. That is what makes gateway and OPA restarts
safe — a restarted OPA re-fetches the current revision instead of running on
whatever it last held in memory — and it keeps apply the single mutation
point. Standard bundle semantics apply: `ETag` carries the revision and a
matching `If-None-Match` gets a 304, so a steady state costs one cheap
request per poll.

## The escalate/bind grant

The operator's ClusterRole holds `escalate` and `bind` on roles and
rolebindings. This is deliberate and worth understanding before changing it:
company namespaces are operator-created, so the orchestrator's bounded
sandbox-provisioner Role *inside* those namespaces must be operator-created
too. The grant is scoped to roles/rolebindings only — the operator still
cannot touch a pod itself.

## Cross-package interactions

- Consumes the `compiler` output carried on the `Company` CR spec
  (`policyBundle`, `platformBundle`, `infra`, `workflows`, `budgets`).
- Writes `companies`, `agent_instances` and `policy_bundles` through
  `registry`; those rows are what the orchestrator, the gateways and the
  egress proxy read at runtime.
- Builds agent pod specs from `common.podsecurity`.
- The CRD definitions themselves live in `crds/` at the repo root.

## Tests

```
uv run pytest packages/operator
```

`test_resources.py` asserts on the built objects directly — no cluster
needed, which is the payoff of keeping the builders pure. `test_reconcile.py`
drives the handlers against a fake `Cluster` and the in-memory registry
doubles. The live behaviour (namespaces really created, NetworkPolicies
really enforced, a fired agent's token really rejected) is proven by
`tests/e2e_kind/test_operator_lifecycle.py`.

## Invariants

- Standing structure only; ephemeral agents are never the operator's.
- Deleting an `AgentInstance` deletes its ServiceAccount, and that is what
  revokes the identity: the API server refuses TokenReview for tokens of an
  SA that no longer exists, so a fired agent's projected token dies with it
  (bounded by the gateway's authn cache TTL).
- Agent pods carry no credential env at all — not even a `secretKeyRef`.
  The pod's only identity is an audience-scoped projected token, and
  `GPG_KEY` (baked into the python base image) is blanked so no
  token-shaped value exists in a company pod.
- The sandbox NetworkPolicy grants **no cluster DNS**. The sandbox needs no
  resolver, and one that forwards unknown names upstream would be an
  exfiltration channel the audited proxy cannot see.
