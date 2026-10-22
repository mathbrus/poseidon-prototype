# reconciler

The reconciler turns the `Company` and `AgentInstance` custom resources
into a running company, and serves the compiled policy bundles OPA
polls. It runs in the platform namespace as two Deployments from one
image, and reacts to the resources the console submits when you apply a
blueprint.

| Kind | Image | Entry point | Holds | Writes |
|---|---|---|---|---|
| service (two processes) | `poseidon-reconciler` | `kopf run -m reconciler.reconcile --all-namespaces`; `python -m reconciler.bundleserver` | `REGISTRY_DSN` | `companies` (the spec), `agent_instances`, `policy_bundles` |

Kubernetes calls a process like this an operator. In Poseidon it is the
reconciler.

## Module map

| Module | Owns |
|---|---|
| `reconcile.py` | The kopf handlers: create, update, resume and delete for both resources, and the startup hook that moves kopf's bookkeeping into annotations. |
| `resources.py` | Every Kubernetes object the reconciler applies, as pure builders. |
| `kube.py` | `Cluster`, the intent-level client. Tests replace this one seam with a fake. |
| `opa.py` | `activated_revision()`, the one read the reconciler makes of OPA: which bundle revision it serves. |
| `bundleserver.py` | The bundle server: `/bundles/{slot}/bundle.tar.gz` for the `platform` and `company` slots, and `/healthz`. |
| `config.py` | `ReconcilerSettings`, every field set by the chart. |

## How it works

### Every event recomputes the whole state

On create, update or resume (kopf's replay at startup), a handler
rebuilds every object from the current spec and upserts it whole.
Nothing reads the event's diff, so a restart or a missed event converges
on the next one.

`reconcile_company` applies, in order:

1. The company namespace, labeled `poseidon.dev/company`.
2. Four NetworkPolicies: `default-deny`, `agent-egress` (the two
   gateways and cluster DNS), `agent-ingress` (the orchestrator worker
   only), and `sandbox-isolation`. The last one is projected from
   `spec.infra.network_policy` and grants no cluster DNS.
3. The `sandbox-provisioner` Role and RoleBinding for the orchestrator
   worker: Jobs, pods, `pods/exec` and ServiceAccounts in this
   namespace.
4. The company and platform bundles, stored in `policy_bundles`.
5. The `companies` row, last, and only once OPA serves both stored
   revisions. The worker starts the heartbeat as soon as this row
   exists.

Until then the handler sets `phase: activating` and raises
`kopf.TemporaryError`, and kopf retries five seconds later. Then it sets
`phase: converged`. `observedGeneration` says which apply a phase
answers for.

`reconcile_instance` creates the agent's ServiceAccount (named by
`agentId`), then its Deployment and Service (named by `standingName`),
and records the agent in `agent_instances` with its namespace. The pod's
only credential is a projected token for the audience
`poseidon-mcp-gateway`. Automount is off, no env var comes from a
secret, and `GPG_KEY` is blanked. Role, repository and vision ride the
pod template, so a blueprint edit rolls out the Deployment.

### Teardown revokes authority first

Deleting a `Company` replaces the stored company bundle with a
tombstone: an empty bundle that still declares the company's roots. OPA
activates it on its next poll and drops the company's rules. The handler
retries until OPA serves the tombstone. Only then does it delete the
`agent_instances` rows for the company, the `companies` row and the
namespace. The platform bundle stays.

Deleting an `AgentInstance` deletes its Deployment, Service and
ServiceAccount, then its registry row. Once the ServiceAccount is gone,
the API server rejects its tokens at TokenReview.

### Policy reaches OPA by poll

The bundle server answers with the stored tarball and an `ETag` equal to
its revision. A matching `If-None-Match` gets a 304. A slot that was
never stored gets a 404. The bundle server connects to the registry on
the first bundle request, so it can start before Postgres accepts
connections.

Two Kubernetes rules shape `kube.py` and `reconcile.py`:

- NetworkPolicies are replaced, never merge-patched. A merge of two
  selector shapes keeps both and deselects every pod, leaving them
  unconfined.
- Status is written through `patch.status`. The CRDs declare a
  structural status schema (`phase`, `bundleRevision`,
  `observedGeneration`, `message`), so the API server prunes kopf's
  default status bookkeeping. The startup hook keeps it in
  `poseidon.dev` annotations instead.

The reconciler's ClusterRole
(`deploy/helm/poseidon/templates/reconciler.yaml`) grants no pods, exec,
jobs or secrets. It holds `escalate` and `bind` on roles and
rolebindings so it can create the worker's sandbox-provisioner Role
without holding those rights itself.

## Invariants

- Reconciling twice converges on the same objects:
  `test_reconcile.py::test_company_reconcile_is_idempotent`.
- A company stays `activating` until OPA serves its bundles:
  `test_company_is_activating_until_opa_serves_its_bundle`.
- Teardown waits for OPA to serve the tombstone:
  `test_company_teardown_waits_until_the_revocation_is_served`.
- Firing an agent deletes its ServiceAccount:
  `test_instance_teardown_removes_everything`.
- Kopf bookkeeping stays out of status:
  `test_startup_keeps_kopf_out_of_status`.
- Agent pods carry no credential env:
  `test_resources.py::test_agent_deployment_carries_no_credential_env_at_all`.
- The pod's identity is the audience-scoped projected token:
  `test_agent_pod_identity_is_the_audience_scoped_projected_token`.
- Agent egress reaches only the two gateways and DNS:
  `test_agent_egress_reaches_only_the_two_gateways_and_dns`.
- The sandbox answers the gateway and reaches only the proxy:
  `test_sandbox_answers_the_gateway_and_reaches_only_the_proxy`.
- The worker's Role is bounded:
  `test_sandbox_provisioner_rbac_is_bounded`.
- A torn-down company serves its tombstone:
  `test_bundleserver.py::test_torn_down_company_serves_its_tombstone`.

## Testing

```bash
uv run pytest packages/reconciler
```

No cluster or database is needed: `test_reconcile.py` drives the
handlers against a fake `Cluster`, registry and OPA. Run tests per
package ([Contributing](../../docs/developing/contributing.md)).

On the cluster, `tests/e2e_kind/test_reconciler_lifecycle.py` applies a
company, checks that OPA activated both bundles, rolls a blueprint edit,
and deletes the company to check that a working credential is refused
afterwards.

## Related

- [How it works](../../docs/explanation/how-it-works.md) and
  [Architecture](../../docs/explanation/architecture.md).
- [OPA](../../docs/concepts/opa.md), [Network
  policy](../../docs/concepts/network-policy.md), [Workload
  identity](../../docs/concepts/workload-identity.md).
- [`compiler`](../compiler/README.md) produces the spec this package
  applies. [`console`](../console/README.md) submits it.
- [`registry`](../registry/README.md) packs the bundles.
  [`orchestrator`](../orchestrator/README.md) creates the ephemeral
  pods.
- The resource definitions are in `crds/` at the repository root.
