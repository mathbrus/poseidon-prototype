"""Deleting the company on kind: it revokes, it erases, and it may purge.

Deleting is a console act. The console holds the heartbeat, ends the
runs, asks the reconciler to tear the company down and deletes its
workflow histories. The reconciler serves the tombstone, removes the
namespace and sweeps every registry row of the company. A credential
that worked before the teardown is refused afterwards. No row of the
company is left in any table, and Temporal knows none of its workflows.
The company applied again starts with no history. With `--purge-remote`,
the repository is reset in place too.

The conftest orders this module after the capstones and before the quiet
check: the delete erases what the capstones read. Every test ends with
the company applied again on the baseline repository, so the quiet check
finds the company it expects.
"""

import asyncio
import io
import json
import secrets
import tarfile
import time
from typing import Any

import httpx
import pytest
from temporalio.client import Client

from common.policy import COMPANY_BUNDLE, company_decision_path, company_package
from common.workflows import heartbeat_run_id, workflow_id
from mcp_gateway.git_backend import EMPTY_TREE

from .blueprints import restore_pristine
from .cluster import (
    COMPANY,
    COMPANY_NS,
    console,
    db_now,
    gateway_url,
    harness_repo_or_fail,
    kubectl,
    port_forward,
    projected_token,
    psql,
    psql_json,
    temporal_port_forward,
)
from .conftest import github_api, open_world_pr, seed_baseline, target_slug

HEARTBEAT = workflow_id("heartbeat", COMPANY)


@pytest.fixture(scope="module", autouse=True)
def company_back(stack: None, baseline_env: dict):
    """Whatever happens in here, the quiet check needs the company back on
    the baseline repository."""
    yield
    seed_baseline(baseline_env)
    restore_pristine()


def company_history() -> dict[str, Any]:
    """What the registry knows of the company before it goes: its run
    ids, the heartbeat's included, and its workflow ids."""
    rows = psql_json(
        "SELECT COALESCE(json_agg(json_build_object("
        "'run_id', run_id, 'workflow_id', workflow_id, 'workflow', workflow)),"
        " '[]'::json) FROM registry.runs WHERE company = :'company'",
        company=COMPANY,
    )
    run_ids = {heartbeat_run_id(COMPANY)} | {row["run_id"] for row in rows}
    workflow_ids = {HEARTBEAT} | {
        row["workflow_id"] or workflow_id(row["workflow"], row["run_id"]) for row in rows
    }
    return {"run_ids": sorted(run_ids), "workflow_ids": sorted(workflow_ids)}


def rows_left(run_ids: list[str]) -> dict[str, int]:
    """Every table's rows of the company, by the sweep's own criteria: the
    company's name, one of its run ids, its heartbeat, its repository."""
    counts = psql(
        "WITH r AS (SELECT unnest(string_to_array(:'runs', ',')) AS run_id)"
        " SELECT json_build_object("
        "'companies', (SELECT count(*) FROM registry.companies"
        "  WHERE name = :'company'),"
        "'agent_instances', (SELECT count(*) FROM registry.agent_instances"
        "  WHERE company = :'company'),"
        "'runs', (SELECT count(*) FROM registry.runs"
        "  WHERE company = :'company' OR run_id IN (SELECT run_id FROM r)),"
        "'run_participants', (SELECT count(*) FROM registry.run_participants"
        "  WHERE company = :'company' OR run_id IN (SELECT run_id FROM r)),"
        "'sandbox_registrations', (SELECT count(*) FROM registry.sandbox_registrations"
        "  WHERE run_id IN (SELECT run_id FROM r)),"
        "'filed_tickets', (SELECT count(*) FROM registry.filed_tickets"
        "  WHERE repo = :'repo' OR run_id IN (SELECT run_id FROM r)),"
        "'budget_ledger', (SELECT count(*) FROM registry.budget_ledger"
        "  WHERE company = :'company' OR run_id IN (SELECT run_id FROM r)),"
        "'audit_events', (SELECT count(*) FROM registry.audit_events"
        "  WHERE company = :'company' OR run_id IN (SELECT run_id FROM r)"
        "     OR workflow = :'heartbeat'))",
        runs=",".join(run_ids),
        company=COMPANY,
        repo=harness_repo_or_fail(),
        heartbeat=HEARTBEAT,
    )
    return json.loads(counts)


def executions_left(workflow_ids: list[str], deadline_s: int = 120) -> list[str]:
    """The workflow ids Temporal still lists an execution of, once its
    asynchronous deletes have had `deadline_s` to land."""

    async def listed(client: Client) -> list[str]:
        found = []
        for wf_id in workflow_ids:
            async for _ in client.list_workflows(f"WorkflowId = '{wf_id}'", limit=1):
                found.append(wf_id)
        return found

    async def poll() -> list[str]:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            deadline = time.monotonic() + deadline_s
            while (left := await listed(client)) and time.monotonic() < deadline:
                await asyncio.sleep(5)
            return left

    return asyncio.run(poll())


def test_a_delete_revokes_and_erases_the_company(stack: None, env: dict):
    # A credential that provably works before the teardown: the standing
    # PM's projected token, exercised through the gateway (the deny is
    # policy answering — authenticated, no binding for this run id).
    agent_token = projected_token(COMPANY_NS, "pm-1")
    mcp = f"{gateway_url(env)}/mcp"
    before = httpx.get(
        mcp, headers={"Authorization": f"Bearer {agent_token}"}, timeout=10
    )
    assert before.status_code != 401, "the pre-teardown credential must work"
    history = company_history()

    # The stop is a console act, and the console answers once the company
    # is gone: its rows swept, its workflow histories deleted.
    deleted = console(env).delete_company("delete: revocation and erasure proof")
    assert deleted.company == COMPANY and deleted.runs_cancelled == []
    assert deleted.purged_remote is False
    assert kubectl("get", "companies", "-o", "name") == ""
    assert kubectl("get", "ns", COMPANY_NS, "--ignore-not-found") == ""

    # Nothing of the company is left in the registry — the delete's own
    # audit row included; the console's answer was the receipt.
    assert rows_left(history["run_ids"]) == dict.fromkeys(
        (
            "companies",
            "agent_instances",
            "runs",
            "run_participants",
            "sandbox_registrations",
            "filed_tickets",
            "budget_ledger",
            "audit_events",
        ),
        0,
    )
    assert executions_left(history["workflow_ids"]) == []

    # Revocation at the delivery layer: the served bundle is a tombstone —
    # the company's roots and no rules under them. The reconciler held the
    # deletion until the running OPA had activated it, so by the time the
    # Company is gone the decision path already answers no document; the
    # platform bundle (the platform's, not the company's) is still served.
    with port_forward("svc/bundle-server", 8081) as port:
        resp = httpx.get(
            f"http://127.0.0.1:{port}/bundles/{COMPANY_BUNDLE}/bundle.tar.gz", timeout=30
        )
        assert resp.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
            assert tar.getnames() == [".manifest"]
            manifest = json.loads(tar.extractfile(".manifest").read())
        assert manifest["roots"] == [company_package(COMPANY).replace(".", "/")]
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        compiled = httpx.get(
            f"{base}/v1/data/{company_decision_path(COMPANY)}", timeout=10
        ).json()
        assert "result" not in compiled, "compiled policy survived teardown"
        platform = httpx.get(
            f"{base}/v1/data/poseidon/platform/decision", timeout=10
        ).json()
        assert "result" in platform

    # And at the gateway: the fired agent's ServiceAccount and instance
    # row died with the namespace, so the pre-teardown token is refused
    # once the gateway's short authn cache (60s) drains — a failed
    # post-teardown call, as the lifecycle demands. Its rejects name no
    # company: the platform's rows, not the company's.
    deadline = time.monotonic() + 120
    while True:
        after = httpx.get(
            mcp, headers={"Authorization": f"Bearer {agent_token}"}, timeout=10
        )
        if after.status_code == 401:
            break
        assert time.monotonic() < deadline, (
            f"pre-teardown credential still accepted ({after.status_code})"
        )
        time.sleep(5)


def test_the_company_applied_again_starts_with_no_history(stack: None, env: dict):
    """A new company under the old name: no runs, no tickets, no spend
    against today's cap, and no tick but its own."""
    fence = db_now()
    restore_pristine()
    assert console(env).runs().runs == []
    assert console(env).tickets().tickets == []
    company = console(env).company()
    assert company.paused is None
    assert company.limits is not None and company.limits.spent_today_usd == 0
    story = psql_json(
        "SELECT COALESCE(json_agg(e), '[]'::json) FROM ("
        " SELECT ts FROM registry.audit_events WHERE run_id = :'run_id'"
        "   AND ts < :'fence') e",
        run_id=heartbeat_run_id(COMPANY),
        fence=fence,
    )
    assert story == [], "the new heartbeat inherited the old one's rows"


def test_a_purge_resets_the_repository(stack: None, env: dict):
    """`purge_remote`: every open pull request and issue closed, every
    branch but main deleted, and main one commit on the empty tree. The
    module's finalizer seeds the baseline again and applies the company."""
    slug = target_slug(env)
    nonce = secrets.token_hex(3)
    pr = open_world_pr(
        env, f"feature/purge-{nonce}", {"PURGE.md": "purge me\n"}, f"purge {nonce}"
    )
    issue = github_api(
        env,
        "POST",
        f"/repos/{slug}/issues",
        json={"title": f"purge {nonce}", "body": "an open issue the purge closes"},
    )
    assert issue.status_code == 201, issue.text

    deleted = console(env).delete_company("delete: purge proof", purge_remote=True)
    assert deleted.purged_remote is True

    assert github_api(env, "GET", f"/repos/{slug}/pulls/{pr}").json()["state"] == "closed"
    number = issue.json()["number"]
    closed = github_api(env, "GET", f"/repos/{slug}/issues/{number}").json()
    assert (closed["state"], closed["state_reason"]) == ("closed", "not_planned")
    # The host's listing lags its writes by up to a minute.
    deadline = time.monotonic() + 120
    while open_items := github_api(
        env, "GET", f"/repos/{slug}/issues?state=open&per_page=100"
    ).json():
        assert time.monotonic() < deadline, [i["number"] for i in open_items]
        time.sleep(5)
    branches = github_api(env, "GET", f"/repos/{slug}/branches?per_page=100").json()
    assert [b["name"] for b in branches] == ["main"]
    commits = github_api(env, "GET", f"/repos/{slug}/commits?sha=main").json()
    assert len(commits) == 1, [c["sha"] for c in commits]
    assert commits[0]["commit"]["tree"]["sha"] == EMPTY_TREE
