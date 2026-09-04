# Workload identity — proving who is calling

A shared secret in an environment variable answers "does the caller know the
password?", which is not the same question as "who is the caller?" Anything
that reads the variable becomes the caller.

Kubernetes offers something better. The kubelet can mount a short-lived,
**audience-scoped** token for a pod's ServiceAccount, and rotate it. A
service receiving that token calls the **TokenReview** API to ask the
cluster who it belongs to. The answer is authoritative and the token is
useless anywhere outside its audience.

Poseidon uses exactly one identity shape: every agent pod — standing or
ephemeral — mounts a projected token for its own ServiceAccount, scoped
to the gateway's audience. What differs is which registry table the
gateway resolves the ServiceAccount against:

- **Standing agents** map through `agent_instances`, the standing agents the
  operator reconciles. The gateway resolves the ServiceAccount name to
  the row and — importantly — requires the recorded namespace to *match*.
  The same SA name in another namespace is an impersonation attempt, and
  is refused and audited.
- **Ephemeral agents** get a per-run ServiceAccount created at
  provision time, mapping through a live `run_participants` row — the
  row *is* the identity: run binding, crew and role, company, namespace. A
  verified token alone is never enough; deleting
  the row revokes the identity **immediately**, however long the token
  itself remains valid.

That last point is the one worth internalising. The TokenReview may be
cached, but the registry row is re-read on **every call**: revocation as
a database write is far stronger than revocation as a short expiry — and
it works the same for firing a standing agent (its row is deleted with
its ServiceAccount) as for sweeping a crew.
