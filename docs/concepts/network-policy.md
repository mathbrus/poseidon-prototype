# NetworkPolicy — the silent backstop

By default every pod in a Kubernetes cluster can reach every other pod.
NetworkPolicy changes that: it is a firewall expressed in terms of pod
labels and namespaces rather than IP addresses.

Poseidon starts from deny-all in each company namespace and punches
specific holes. A sandbox may receive connections from the MCP gateway, and
may make connections to the egress proxy. That is the entire list.

The platform namespace carries fences of its own for the surfaces that
are authority: the Temporal frontend admits the orchestrator worker, the
console and Temporal's own pods and nothing else, and each chokepoint's
egress names what it may reach. A fence stops pods; it does not stop a
person with cluster credentials, whose `kubectl port-forward` enters a
pod's own network namespace beneath any policy. What guards the frontend
against that road is the verified verdict, not the fence.

Two things follow that are easy to miss:

1. **It needs a CNI that actually enforces it.** kind's default network
   plugin silently ignores NetworkPolicy, which would make every containment
   test pass for the wrong reason. The bootstrap installs Calico instead.
2. **It drops packets silently**, which makes it a poor *audit* layer. That
   is precisely why the egress proxy exists on top: NetworkPolicy is the
   backstop, and the proxy is what makes a blocked attempt *visible*.
