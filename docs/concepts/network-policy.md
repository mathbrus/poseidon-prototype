# NetworkPolicy — the silent backstop

By default every pod in a Kubernetes cluster can reach every other pod.
NetworkPolicy changes that: it is a firewall expressed in terms of pod
labels and namespaces rather than IP addresses.

Poseidon starts from deny-all in each company namespace and punches
specific holes. A sandbox may receive connections from the MCP gateway, and
may make connections to the egress proxy. That is the entire list.

Two things follow that are easy to miss:

1. **It needs a CNI that actually enforces it.** kind's default network
   plugin silently ignores NetworkPolicy, which would make every containment
   test pass for the wrong reason. The bootstrap installs Calico instead.
2. **It drops packets silently**, which makes it a poor *audit* layer. That
   is precisely why the egress proxy exists on top: NetworkPolicy is the
   backstop, and the proxy is what makes a blocked attempt *visible*.
