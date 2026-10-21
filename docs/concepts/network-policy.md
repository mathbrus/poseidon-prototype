# NetworkPolicy

By default, every pod in a Kubernetes cluster can reach every other pod.
A [NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
changes that. It is a firewall written in terms of pod labels and
namespaces instead of IP addresses.

## How Poseidon uses it

The company namespace starts from deny-all, then opens specific paths. A
sandbox accepts connections from the MCP gateway, and opens connections
to the egress proxy. Nothing else.

The platform namespace has its own rules. For example, the Temporal
frontend admits only the orchestrator, the console and Temporal's own
pods.

## Two things to know

1. **It needs a network plugin that enforces it.** kind's default plugin
   ignores NetworkPolicy, which would make every containment test pass for
   the wrong reason. `kind-up.sh` installs Calico instead.
2. **It drops packets silently.** That makes it a poor audit layer, so the
   egress proxy sits on top of it and records every attempt. NetworkPolicy
   is the backstop. The proxy is what makes a blocked attempt visible.

A NetworkPolicy stops pods. It doesn't stop a person with cluster
credentials, whose `kubectl port-forward` enters a pod's network directly.
[Limitations](../explanation/limitations.md#some-services-rely-on-networkpolicy-alone)
covers what that leaves open.
