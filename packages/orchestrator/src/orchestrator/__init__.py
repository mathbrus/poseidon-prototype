"""Poseidon orchestrator: durable workflows over Temporal.

Layout:
    cli/        `poseidon run|signal|status` — a thin Temporal client
    workflows/  workflow definitions (scope, implement, review, heartbeat)
    activities/ step-shaped activity functions, thin clients over the
                gateway, SandboxBackend and agent HTTP
    gates/      process gates (signal handler, escalation timer,
                audit-event shaping) — the review workflow's merge gate
                and the implement workflow's budget pause ride these
    worker/     the worker process (Deployment in the platform namespace)

The worker holds no GIT_TOKEN and no provider keys; its gateway identity
is a projected service-account token, audience-scoped to
the gateway and rotated by the kubelet.
"""
