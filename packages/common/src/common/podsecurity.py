"""The pod security posture every Poseidon workload runs under.

One definition for the three builders that emit pod specs from Python —
the sandbox Job manifest, the orchestrator's ephemeral pods and the operator's
standing-agent Deployments — so tightening the posture cannot silently
skip one of them.

`RUN_AS_UID` is the user baked into every Poseidon image (see the
`useradd` line in the root Dockerfile); the two must move
together. The Helm chart carries its own copy in `_helpers.tpl` because
templates cannot import Python.

The root filesystem is deliberately not made read-only here: the
workloads that use these constants all legitimately write (workspace
builds in the sandbox, SDK state under $HOME in the agents). Chart-managed
platform services, which do not, set `readOnlyRootFilesystem` themselves.
"""

RUN_AS_UID = 10001

POD_SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": RUN_AS_UID,
    "runAsGroup": RUN_AS_UID,
    "seccompProfile": {"type": "RuntimeDefault"},
}

CONTAINER_SECURITY_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
}
