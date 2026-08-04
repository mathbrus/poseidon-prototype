"""Poseidon orchestrator (§4.2): durable workflows over Temporal.

Layout:
    cli/        `poseidon run|signal|status` — a thin Temporal client
    workflows/  workflow definitions (implement)
    activities/ step-shaped activity functions, thin clients over the
                gateway, SandboxBackend and agent HTTP
    gates/      process-gate skeleton (signal handler, escalation timer,
                audit-event stub) — nothing wired into implement at M1
    worker/     the worker process (Deployment in the platform namespace)

The worker holds no GIT_TOKEN and no provider keys; its gateway identity
is a projected service-account token (plan step 11), audience-scoped to
the gateway and rotated by the kubelet.
"""
