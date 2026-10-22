"""The console interface, rendered and held still.

Renders the console's OpenAPI document from the app into the committed
`packages/console/openapi.json`, and one golden example per route — the
request a client sends and the answer it gets — into
`packages/console/examples/`, from a console over the in-memory fakes
(`console.fakes`) seeded the same way every time. The CLI's tests replay
the examples, so a change to the interface fails them in the same pull
request; CI runs `--check`, so a change nobody rendered fails there.

    scripts/console_interface.py --write          render both into the tree
    scripts/console_interface.py --check          fail if either drifted
    scripts/console_interface.py --breaking REF   compare with the snapshot at
                                                  the merge base with REF
        [--against REV]                           ...the snapshot committed at REV,
                                                  not the console's rendering

`--breaking` prints `breaking`, `additive`, `unchanged` or `introduced`
on its first line, then what changed; it exits 1 when the change is
breaking: a route removed, a field removed or retyped, a request field
newly required, a response field no longer always present, an enum
narrowed.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from common.audit import HEARTBEAT_TICK, REVIEW_VERDICT, AuditEvent, gate_event
from common.console import ROUTES, Route
from common.tasks import issue_run_task, review_run_task
from common.topology import COMPANY_NAMESPACE, PHASE_CONVERGED
from common.version import VERSION_VAR
from common.workflows import (
    GATE_PARKED,
    MERGE_GATE,
    PARKED_STATE,
    TICK_IDLE,
    TICK_STARTED,
    gate_rule,
    heartbeat_run_id,
    tick_rule,
    workflow_id,
)
from console.fakes import COMPANY, World, applied_spec, as_owner, company_cr

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "packages" / "console" / "openapi.json"
EXAMPLES = ROOT / "packages" / "console" / "examples"
SNAPSHOT_PATH = SNAPSHOT.relative_to(ROOT).as_posix()
HELLO_SAAS = (ROOT / "examples" / "hello-saas.yaml").read_text()

REPO = "https://github.com/acme/hello-saas"
IMPLEMENTER = "feature-delivery-implementer-run-01implement"
REVIEWER = "feature-review-reviewer-run-01review"
IMPLEMENT_RUN = "run-01IMPLEMENT"
REVIEW_RUN = "run-01REVIEW"
STARTED_RUN = "run-01STARTED"


# -- the seeded world ----------------------------------------------------------


def _row(ts: str, run_id: str, **fields: Any) -> AuditEvent:
    return AuditEvent(
        ts=ts,
        run_id=run_id,
        args_digest="0" * 64,
        latency_ms=fields.pop("latency_ms", 4),
        backend_status=fields.pop("backend_status", None),
        company=COMPANY,
        **fields,
    )


def seeded() -> World:
    """One company, converged: a delivered implement run, a review run
    parked on its merge gate, a sleeping heartbeat with two ticks, the
    crews, the ticket the company filed, the rows and the spend behind
    them."""
    world = World()
    world.cr = company_cr(
        COMPANY,
        generation=2,
        phase=PHASE_CONVERGED,
        observedGeneration=2,
        bundleRevision="9f2c41d0",
        message="",
    )
    world.cr["spec"]["blueprintSource"] = {
        "text": HELLO_SAAS,
        "sha256": hashlib.sha256(HELLO_SAAS.encode()).hexdigest(),
        "appliedBy": "owner",
        "appliedAt": "2025-12-31T23:00:00+00:00",
    }
    world.instances.upsert("pm-1", COMPANY, "pm", "pm", namespace=COMPANY_NAMESPACE)

    runs = world.runs
    runs.start(
        IMPLEMENT_RUN,
        issue_run_task(7),
        "implement",
        workflow_id("implement", IMPLEMENT_RUN),
        COMPANY,
    )
    runs.record_artifacts(IMPLEMENT_RUN, f"{REPO}/pull/12", [f"{REPO}/issues/7"])
    runs.finish(IMPLEMENT_RUN, "done", iterations=3)
    runs.rows[IMPLEMENT_RUN].created_at = "2025-12-31T23:40:00+00:00"
    runs.start(
        REVIEW_RUN,
        review_run_task(12),
        "review",
        workflow_id("review", REVIEW_RUN),
        COMPANY,
    )
    runs.record_verdict(REVIEW_RUN, "pass")
    runs.rows[REVIEW_RUN].created_at = "2025-12-31T23:55:00+00:00"
    world.workflows.running[workflow_id("review", REVIEW_RUN)] = PARKED_STATE["merge"]
    world.workflows.running[workflow_id("heartbeat", COMPANY)] = "sleeping"
    world.run_ids = [STARTED_RUN]

    world.documents.files[applied_spec()["documents"]["strategy"]] = (
        "# Strategy\n\nShip slugify first: every later slice builds on it.\n"
    )
    world.participants.add(
        IMPLEMENTER, IMPLEMENT_RUN, "pm-1", "feature-delivery", "implementer", COMPANY
    )
    world.participants.terminate(IMPLEMENTER)
    world.participants.add(
        REVIEWER, REVIEW_RUN, "pm-1", "feature-review", "reviewer", COMPANY
    )
    filed_at = datetime.fromisoformat("2025-12-31T23:30:00+00:00").timestamp()
    world.tickets.rows[(applied_spec()["repo"]["url"], 7)] = (
        heartbeat_run_id(COMPANY),
        filed_at,
    )

    heartbeat = heartbeat_run_id(COMPANY)
    platform = {"agent_id": "orchestrator", "role": "platform", "lifecycle": "platform"}
    for event in (
        _row(
            "2025-12-31T23:39:00+00:00",
            heartbeat,
            tool=HEARTBEAT_TICK,
            decision="allow",
            rule=tick_rule(TICK_STARTED, f"implement#7_as_{IMPLEMENT_RUN}"),
            source="process-lifecycle",
            workflow=workflow_id("heartbeat", COMPANY),
            **platform,
        ),
        _row(
            "2025-12-31T23:41:00+00:00",
            IMPLEMENT_RUN,
            agent_id=IMPLEMENTER,
            role="implementer",
            tool="git.open_pr",
            decision="allow",
            rule="crew_feature_delivery_implementer_git_open_pr",
            rationale="slugify handles unicode, as the ticket asks",
            backend_status="ok",
            source="gateway",
            lifecycle="ephemeral",
            parent_agent_id="pm-1",
            workflow=workflow_id("implement", IMPLEMENT_RUN),
        ),
        _row(
            "2025-12-31T23:56:00+00:00",
            REVIEW_RUN,
            tool=REVIEW_VERDICT,
            decision="allow",
            rule="review_pass",
            rationale="the change does what the ticket asks; tests cover it",
            source="process-lifecycle",
            workflow=workflow_id("review", REVIEW_RUN),
            **platform,
        ),
        _row(
            "2025-12-31T23:57:00+00:00",
            REVIEW_RUN,
            tool=gate_event(MERGE_GATE),
            decision="deny",
            rule=gate_rule(GATE_PARKED),
            source="process-gate",
            workflow=workflow_id("review", REVIEW_RUN),
            **platform,
        ),
        _row(
            "2025-12-31T23:58:00+00:00",
            heartbeat,
            tool=HEARTBEAT_TICK,
            decision="allow",
            rule=tick_rule(TICK_IDLE, "max_concurrent_runs"),
            source="process-lifecycle",
            workflow=workflow_id("heartbeat", COMPANY),
            **platform,
        ),
    ):
        world.audit.append(event)
    world.ledger.entries.append(
        {
            "ts": "2025-12-31T23:41:30+00:00",
            "company": COMPANY,
            "run_id": IMPLEMENT_RUN,
            "agent_id": IMPLEMENTER,
            "parent_agent_id": "pm-1",
            "role": "implementer",
            "model": "mock",
            "prompt_tokens": 1830,
            "completion_tokens": 212,
            "cached_tokens": 1024,
            "reasoning_tokens": 0,
            "usd": 0.0042,
        }
    )
    return world


# -- one request per route -----------------------------------------------------

# What a client sends to each route: the concrete path, the query and the
# body. The seeded world makes every one of them answer 200.
REQUESTS: dict[tuple[str, str], tuple[str, dict[str, Any], dict[str, Any] | None]] = {
    ("GET", "/healthz"): ("/healthz", {}, None),
    ("GET", "/whoami"): ("/whoami", {}, None),
    ("POST", "/blueprint/preview"): ("/blueprint/preview", {}, {"blueprint": HELLO_SAAS}),
    ("POST", "/blueprint/apply"): ("/blueprint/apply", {}, {"blueprint": HELLO_SAAS}),
    ("GET", "/blueprint"): ("/blueprint", {}, None),
    ("GET", "/company"): ("/company", {}, None),
    ("GET", "/company/bounds"): ("/company/bounds", {}, None),
    ("GET", "/documents/{kind}"): ("/documents/strategy", {}, None),
    ("GET", "/agents"): ("/agents", {}, None),
    ("GET", "/tickets"): ("/tickets", {}, None),
    ("DELETE", "/company"): (
        "/company",
        {},
        {"rationale": "the vision is done", "wait": True},
    ),
    ("POST", "/runs"): ("/runs", {}, {"workflow": "implement", "number": 7}),
    ("GET", "/runs"): ("/runs", {}, None),
    ("GET", "/runs/{run_id}"): (f"/runs/{REVIEW_RUN}", {}, None),
    ("GET", "/runs/{run_id}/story"): (f"/runs/{REVIEW_RUN}/story", {}, None),
    ("GET", "/runs/{run_id}/spend"): (f"/runs/{IMPLEMENT_RUN}/spend", {}, None),
    ("GET", "/runs/{run_id}/crew"): (f"/runs/{REVIEW_RUN}/crew", {}, None),
    ("POST", "/runs/{run_id}/gates/{gate}"): (
        f"/runs/{REVIEW_RUN}/gates/merge",
        {},
        {"approved": True, "rationale": "engine first, as the vision asks"},
    ),
    ("POST", "/runs/{run_id}/cancel"): (
        f"/runs/{REVIEW_RUN}/cancel",
        {},
        {"rationale": "superseded by #13"},
    ),
    ("GET", "/heartbeat"): ("/heartbeat", {}, None),
    ("POST", "/heartbeat/wake"): ("/heartbeat/wake", {}, None),
    ("POST", "/heartbeat/pause"): (
        "/heartbeat/pause",
        {},
        {"rationale": "reading the ledger"},
    ),
    ("POST", "/heartbeat/resume"): ("/heartbeat/resume", {}, {"rationale": "read it"}),
    ("GET", "/spend"): ("/spend", {}, None),
    ("GET", "/events"): ("/events", {"after": 0}, None),
    ("GET", "/now"): ("/now", {}, None),
    ("GET", "/acts"): ("/acts", {}, None),
    ("GET", "/runs/{run_id}/acts"): (f"/runs/{REVIEW_RUN}/acts", {}, None),
}

# A world to prepare before a route's request, beyond the seed.
PREPARE: dict[tuple[str, str], Callable[[World], Awaitable[None] | None]] = {
    ("POST", "/heartbeat/resume"): lambda w: w.companies.pause(COMPANY, "owner"),
}


def example_name(entry: Route) -> str:
    """`GET /runs/{run_id}/story` → `GET_runs_run_id_story.json`."""
    slug = entry.path.strip("/").replace("{", "").replace("}", "").replace("/", "_")
    return f"{entry.method}_{slug or 'root'}.json"


async def _example(entry: Route) -> dict[str, Any]:
    world = seeded()
    world.patch(setattr)
    prepare = PREPARE.get((entry.method, entry.path))
    if prepare is not None:
        prepared = prepare(world)
        if asyncio.iscoroutine(prepared):
            await prepared
    path, params, body = REQUESTS[(entry.method, entry.path)]
    async with world.client() as client:
        response = await client.request(
            entry.method, path, params=params, json=body, headers=as_owner()
        )
    if response.status_code != 200:
        raise SystemExit(
            f"console_interface: {entry.method} {path} answered"
            f" {response.status_code}: {response.text}"
        )
    answer = response.json()
    entry.response.model_validate(answer)
    # The blueprint's text is the example's own file; the request shows
    # where it goes, not the whole document again.
    shown = (
        {"blueprint": "<the text of examples/hello-saas.yaml>"}
        if body and "blueprint" in body
        else body
    )
    return {
        "route": f"{entry.method} {entry.path}",
        "request": {"path": path, "query": params, "body": shown},
        "status": response.status_code,
        "response": answer,
    }


def render_examples() -> dict[str, str]:
    missing = {(r.method, r.path) for r in ROUTES} - set(REQUESTS)
    if missing:
        raise SystemExit(f"console_interface: no example request for {sorted(missing)}")

    async def all_of() -> dict[str, str]:
        return {example_name(entry): _dump(await _example(entry)) for entry in ROUTES}

    return asyncio.run(all_of())


# -- the OpenAPI document ------------------------------------------------------


def render_openapi() -> str:
    """The app's OpenAPI document. Its version is the running release's
    (`GET /healthz` answers it), so the snapshot, rendered from a
    checkout, says `unreleased`: the interface has no version of its own."""
    os.environ.pop(VERSION_VAR, None)
    document = World().app().openapi()
    return _dump(document)


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


# -- write and check -----------------------------------------------------------


def rendered() -> dict[Path, str]:
    files = {SNAPSHOT: render_openapi()}
    files.update({EXAMPLES / name: text for name, text in render_examples().items()})
    return files


def write() -> None:
    files = rendered()
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    for stale in set(EXAMPLES.glob("*.json")) - set(files):
        stale.unlink()
    for path, text in files.items():
        path.write_text(text)
    print(f"wrote {SNAPSHOT_PATH} and {len(files) - 1} examples")


def check() -> int:
    files = rendered()
    drifted = []
    for path, text in files.items():
        current = path.read_text() if path.exists() else ""
        if current != text:
            drifted.append(path)
            sys.stdout.writelines(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    f"{path.relative_to(ROOT)} (committed)",
                    f"{path.relative_to(ROOT)} (rendered)",
                )
            )
    extra = set(EXAMPLES.glob("*.json")) - set(files)
    for path in sorted(extra):
        drifted.append(path)
        print(f"{path.relative_to(ROOT)}: no route renders it")
    if drifted:
        print(
            f"console_interface: {len(drifted)} file(s) drifted from the console;"
            " run `uv run python scripts/console_interface.py --write` and review"
            " the diff — a change to the console interface is a reviewed change"
        )
        return 1
    print("console interface: snapshot and examples match the console")
    return 0


# -- breaking changes ----------------------------------------------------------


def _resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        return document["components"]["schemas"][ref.rsplit("/", 1)[1]]
    return schema


def _shape(document: dict[str, Any], schema: dict[str, Any]) -> Any:
    """A schema reduced to what a client depends on: types, refs by name,
    enums as sets — descriptions, titles and defaults dropped."""
    if "$ref" in schema:
        return ("ref", schema["$ref"].rsplit("/", 1)[1])
    if "anyOf" in schema:
        return ("anyOf", sorted(json.dumps(_shape(document, s)) for s in schema["anyOf"]))
    if "enum" in schema:
        return ("enum",)
    if schema.get("type") == "array":
        return ("array", _shape(document, schema.get("items", {})))
    if schema.get("type") == "object" and "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        return ("map", _shape(document, extra) if isinstance(extra, dict) else extra)
    return (schema.get("type"), schema.get("format"))


def _used_schemas(document: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The schema names a request reaches and those a response reaches."""

    def walk(schema: Any, seen: set[str]) -> None:
        if isinstance(schema, dict):
            ref = schema.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[1]
                if name not in seen:
                    seen.add(name)
                    walk(document["components"]["schemas"][name], seen)
            for value in schema.values():
                walk(value, seen)
        elif isinstance(schema, list):
            for value in schema:
                walk(value, seen)

    requests: set[str] = set()
    responses: set[str] = set()
    for operations in document.get("paths", {}).values():
        for operation in operations.values():
            walk(operation.get("requestBody"), requests)
            walk(operation.get("parameters"), requests)
            walk(operation.get("responses"), responses)
    return requests, responses


def compare(old: dict[str, Any], new: dict[str, Any]) -> tuple[list[str], list[str]]:
    """What breaks a client written against `old`, and what `new` adds."""
    breaking: list[str] = []
    additive: list[str] = []

    old_routes = {(m.upper(), p) for p, ops in old.get("paths", {}).items() for m in ops}
    new_routes = {(m.upper(), p) for p, ops in new.get("paths", {}).items() for m in ops}
    breaking += [f"route removed: {m} {p}" for m, p in sorted(old_routes - new_routes)]
    additive += [f"route added: {m} {p}" for m, p in sorted(new_routes - old_routes)]

    old_schemas = old.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})
    old_requests, old_responses = _used_schemas(old)
    for name, before in sorted(old_schemas.items()):
        after = new_schemas.get(name)
        if after is None:
            breaking.append(f"model removed: {name}")
            continue
        if "enum" in before:
            lost = set(before["enum"]) - set(after.get("enum", []))
            gained = set(after.get("enum", [])) - set(before["enum"])
            if lost:
                breaking.append(f"{name}: values removed {sorted(lost)}")
            if gained:
                additive.append(f"{name}: values added {sorted(gained)}")
            continue
        in_request = name in old_requests
        in_response = name in old_responses
        before_props = before.get("properties", {})
        after_props = after.get("properties", {})
        before_required = set(before.get("required", []))
        after_required = set(after.get("required", []))
        for field, schema in sorted(before_props.items()):
            if field not in after_props:
                breaking.append(f"{name}.{field}: removed")
            elif _shape(old, schema) != _shape(new, after_props[field]):
                breaking.append(f"{name}.{field}: retyped")
            elif in_response and field in before_required - after_required:
                breaking.append(f"{name}.{field}: no longer always present")
            elif in_request and field in after_required - before_required:
                breaking.append(f"{name}.{field}: now required")
        for field in sorted(set(after_props) - set(before_props)):
            if in_request and field in after_required:
                breaking.append(f"{name}.{field}: added, required")
            else:
                additive.append(f"{name}.{field}: added")
    additive += [f"model added: {n}" for n in sorted(set(new_schemas) - set(old_schemas))]
    return breaking, additive


def _merge_base(ref: str, against: str) -> str:
    return subprocess.run(
        ["git", "merge-base", ref, against],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _snapshot_at(commit: str) -> dict[str, Any] | None:
    shown = subprocess.run(
        ["git", "show", f"{commit}:{SNAPSHOT_PATH}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def breaking(ref: str, against: str | None = None) -> int:
    base = _merge_base(ref, against or "HEAD")
    old = _snapshot_at(base)
    if old is None:
        print("introduced")
        print(f"  no {SNAPSHOT_PATH} at {ref} ({base[:7]}): the interface is new")
        return 0
    new = _snapshot_at(against) if against else json.loads(render_openapi())
    if new is None:
        raise SystemExit(f"console_interface: no {SNAPSHOT_PATH} at {against}")
    found, added = compare(old, new)
    verdict = "breaking" if found else "additive" if added else "unchanged"
    print(verdict)
    for line in found:
        print(f"  breaking: {line}")
    for line in added:
        print(f"  additive: {line}")
    return 1 if found else 0


def main() -> None:
    # The console logs every act it audits; rendering is not acting.
    logging.disable(logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--breaking", metavar="REF")
    parser.add_argument("--against", metavar="REV")
    args = parser.parse_args()
    if args.against and not args.breaking:
        parser.error("--against goes with --breaking")
    if args.write:
        write()
    elif args.check:
        raise SystemExit(check())
    else:
        raise SystemExit(breaking(args.breaking, args.against))


if __name__ == "__main__":
    main()
