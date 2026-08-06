"""OCSF → step-13 audit contract (plan step 24).

OpenShell emits OCSF v1.7 events for what happens inside a sandbox. The
plan's rule is that this becomes an **ingest adapter into our audit
contract, never a parallel silo** (§4.6): one `run_id` query must return
the whole story, whichever runtime produced it.

So this module is a pure function — OCSF record in, `AuditEvent` out —
with no I/O and no store of its own. The caller (an ingest job) reads
OpenShell's JSONL and appends through the ordinary `AuditStore`, exactly
as the egress proxy does.

Two honest limits, both O-5 findings rather than implementation details:

1. **Attribution is ours to supply.** OCSF records carry no Poseidon
   `run_id`; the link is the sandbox's label, which the reader knows from
   the sandbox it is draining. `run_id` is therefore a parameter here, not
   something parsed out of the record — inventing it from a hostname would
   be attribution theatre.
2. **The source of truth sits inside the audited thing.** OpenShell writes
   these records to a file *in the sandbox* (`/var/log/openshell-ocsf.*`,
   3 files retained), i.e. the workload being audited can reach its own
   audit trail. Our own producers write to Postgres from outside the
   sandbox. Mapping the shape is easy; that trust asymmetry is the finding.
"""

from typing import Any

from common.audit import AuditEvent

# OCSF class_uid → the tool name a Poseidon reader recognises. Only the
# classes OpenShell actually emits are mapped; anything else is refused
# rather than guessed at, so an unmapped class shows up as a gap instead
# of a plausible-looking row.
CLASS_TO_TOOL: dict[int, str] = {
    4001: "sandbox.network",       # Network Activity
    4002: "sandbox.http",          # HTTP Activity
    4007: "sandbox.ssh",           # SSH Activity
    1007: "sandbox.process",       # Process Activity
    2004: "sandbox.finding",       # Detection Finding
    6008: "sandbox.config_change",  # Config State Change
    6002: "sandbox.app_lifecycle",  # Application Lifecycle
}

# OCSF disposition_id semantics: 1=Allowed, 2=Blocked (the two OpenShell
# uses for policy outcomes). Anything else is treated as an allow with the
# disposition preserved in the rule, because silently calling an unknown
# outcome "deny" would overstate what was enforced.
BLOCKED_DISPOSITIONS = {2, 3, 4}  # Blocked, Quarantined, Isolated


class UnmappedOcsfClass(ValueError):
    """An OCSF class this adapter has no mapping for. Refused loudly: an
    audit store that silently drops or mislabels events is worse than one
    that admits the gap."""


def to_audit_event(
    record: dict[str, Any],
    *,
    run_id: str,
    agent_id: str = "openshell-sandbox",
    company: str = "",
) -> AuditEvent:
    """Translate one OCSF record into the v2 audit contract.

    `run_id` is supplied by the caller (see the module docstring): the
    record itself cannot attribute the event to a Poseidon run.
    """
    class_uid = record.get("class_uid")
    tool = CLASS_TO_TOOL.get(class_uid)
    if tool is None:
        raise UnmappedOcsfClass(
            f"OCSF class_uid {class_uid!r} has no Poseidon tool mapping"
        )

    disposition = record.get("disposition_id")
    blocked = disposition in BLOCKED_DISPOSITIONS
    # `rule` names why, in the vocabulary a Poseidon reader already knows
    # from the egress proxy's events: the enforcement point plus outcome.
    rule = record.get("rule", {}).get("name") if isinstance(
        record.get("rule"), dict
    ) else record.get("rule")
    if not rule:
        rule = "openshell_blocked" if blocked else "openshell_allowed"

    return AuditEvent(
        ts=_timestamp(record),
        run_id=run_id,
        agent_id=agent_id,
        # v2 (§5.2) puts the enforcement point in `source`; role stays
        # empty for infrastructure producers, as the egress proxy does.
        role="",
        tool=tool,
        args_digest=_digest(record),
        decision="deny" if blocked else "allow",
        rule=rule,
        latency_ms=int(record.get("duration") or 0),
        backend_status=None if blocked else "ok",
        source="sandbox-infra",
        company=company,
        lifecycle="ephemeral",
        parent_agent_id="",
        workflow="",
    )


def _timestamp(record: dict[str, Any]) -> str:
    """OCSF carries both a formatted `time_dt` and an epoch `time`; prefer
    the formatted one and fall back rather than fabricating a clock read —
    an ingested event must keep the time it happened, not the time it was
    drained."""
    stamped = record.get("time_dt")
    if isinstance(stamped, str) and stamped:
        return stamped
    epoch = record.get("time")
    if isinstance(epoch, (int, float)):
        from datetime import datetime, timezone

        # OCSF `time` is milliseconds since the epoch.
        return datetime.fromtimestamp(epoch / 1000, tz=timezone.utc).isoformat()
    raise UnmappedOcsfClass("OCSF record carries no usable timestamp")


def _digest(record: dict[str, Any]) -> str:
    """The same discipline as every other producer: arguments are digested,
    never stored — audit must not become a leak channel (§5.2). The digest
    covers the record minus its volatile envelope so the same observed
    action digests alike."""
    import hashlib
    import json

    volatile = {"time", "time_dt", "duration", "metadata"}
    payload = {k: v for k, v in sorted(record.items()) if k not in volatile}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
