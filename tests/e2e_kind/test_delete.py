"""Deleting the company on kind: it revokes, it erases, and it may purge.

Deleting is a console act. The console holds the heartbeat, ends the
runs, asks the reconciler to tear the company down and deletes its
workflow histories. The reconciler serves the tombstone, removes the
namespace and sweeps every registry row of the company. A credential
that worked before the teardown is refused afterwards. No row of the
company is left in any table, and Temporal knows none of its workflows.
The company applied again starts with no history. With `--purge-remote`,
the repository is reset in place too.

One delete carries every claim: it purges as it erases. The conftest
orders this module after the capstones and before the quiet check: the
delete erases what the capstones read. The company is applied again on the
baseline repository before the module ends, so the quiet check finds the
company it expects.
"""

import asyncio
import io
import json
import secrets
import tarfile
import time

import httpx
import pytest
from temporalio.client import Client

from common.policy import COMPANY_BUNDLE, company_decision_path, company_package
from common.workflows import TASK_QUEUE, heartbeat_run_id
from mcp_gateway.git_backend import EMPTY_TREE

from .blueprints import restore_pristine
from .cluster import (
    COMPANY,
    COMPANY_NS,
    HEARTBEAT_ID,
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
from .conftest import file_ticket, github_api, open_world_pr, seed_baseline, target_slug


@pytest.fixture(scope="module", autouse=True)
def company_back(stack: None, baseline_env: dict):
    """Whatever happens in here, the quiet check needs the company back on
    the baseline repository."""
    yield
    seed_baseline(baseline_env)
    restore_pristine()


def company_run_ids() -> list[str]:
    """The company's run ids, its heartbeat's included, as the registry
    knows them before the company goes."""
    rows = psql_json(
        "SELECT COALESCE(json_agg(run_id), '[]'::json) FROM registry.runs"
        " WHERE company = :'company'",
        company=COMPANY,
    )
    return sorted({heartbeat_run_id(COMPANY), *rows})


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
        heartbeat=HEARTBEAT_ID,
    )
    return json.loads(counts)


def executions_left(deadline_s: int = 120) -> list[str]:
    """The workflow ids Temporal still lists on the worker's task queue,
    once its asynchronous deletes have had `deadline_s` to land."""

    async def poll() -> list[str]:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            deadline = time.monotonic() + deadline_s
            while True:
                left = [
                    e.id
                    async for e in client.list_workflows(f"TaskQueue = '{TASK_QUEUE}'")
                ]
                if not left or time.monotonic() >= deadline:
                    return left
                await asyncio.sleep(5)

    return asyncio.run(poll())


@pytest.fixture(scope="module")
def deleted(stack: None, env: dict) -> dict:
    """One delete, with `purge_remote`, of a company that has a credential
    in use and a repository with something open on it: a pull request and
    an issue the world filed. Everything the assertions read about the
    company is taken before it goes. The heartbeat is held first, so the
    open issue never becomes a run; the issue counts as filed only once the
    git host lists it, as the purge reads the listing."""
    slug = target_slug(env)
    nonce = secrets.token_hex(3)
    console(env).pause("delete: nothing new starts before the company goes")
    pr = open_world_pr(
        env, f"feature/purge-{nonce}", {"PURGE.md": "purge me\n"}, f"purge {nonce}"
    )
    issue = file_ticket(env, f"purge {nonce}", "an open issue the purge closes")
    # A credential that provably works before the teardown: the standing
    # PM's projected token, exercised through the gateway (the deny is
    # policy answering — authenticated, no binding for this run id).
    agent_token = projected_token(COMPANY_NS, "pm-1")
    mcp = f"{gateway_url(env)}/mcp"
    before = httpx.get(
        mcp, headers={"Authorization": f"Bearer {agent_token}"}, timeout=10
    )
    assert before.status_code != 401, "the pre-teardown credential must work"
    run_ids = company_run_ids()
    # The stop is a console act, and the console answers once the company
    # is gone: its rows swept, its workflow histories deleted, its
    # repository reset.
    answer = console(env).delete_company(
        "delete: revocation, erasure and purge proof", purge_remote=True
    )
    # The env (and its token) stays out of what a failing test prints.
    return {
        "slug": slug,
        "pr": pr,
        "issue": issue,
        "agent_token": agent_token,
        "mcp": mcp,
        "run_ids": run_ids,
        "answer": answer,
    }


def test_a_delete_revokes_and_erases_the_company(deleted: dict):
    answer = deleted["answer"]
    assert answer.company == COMPANY and answer.runs_cancelled == []
    assert answer.purged_remote is True
    assert kubectl("get", "companies", "-o", "name") == ""
    assert kubectl("get", "ns", COMPANY_NS, "--ignore-not-found") == ""

    # Nothing of the company is left in the registry — the delete's own
    # audit row included; the console's answer was the receipt.
    assert rows_left(deleted["run_ids"]) == dict.fromkeys(
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
    assert executions_left() == []

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
    headers = {"Authorization": f"Bearer {deleted['agent_token']}"}
    deadline = time.monotonic() + 120
    while True:
        after = httpx.get(deleted["mcp"], headers=headers, timeout=10)
        if after.status_code == 401:
            break
        assert time.monotonic() < deadline, (
            f"pre-teardown credential still accepted ({after.status_code})"
        )
        time.sleep(5)


def test_a_purge_resets_the_repository(deleted: dict, env: dict):
    """`purge_remote`: every open pull request and issue closed, every
    branch but main deleted, and main one commit on the empty tree."""
    slug = deleted["slug"]
    pull = github_api(env, "GET", f"/repos/{slug}/pulls/{deleted['pr']}").json()
    assert pull["state"] == "closed"
    closed = github_api(env, "GET", f"/repos/{slug}/issues/{deleted['issue']}").json()
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


def test_the_company_applied_again_starts_with_no_history(deleted: dict, env: dict):
    """A new company under the old name inherits nothing: no run, ticket,
    spend or audit row of it predates the apply, so its day starts at $0
    against the daily cap. What its own first ticks do is its own. The
    purged repository gets its baseline back first."""
    seed_baseline(env)
    fence = db_now()
    restore_pristine()
    assert console(env).company().paused is None
    older = psql(
        "SELECT json_build_object("
        "'runs', (SELECT count(*) FROM registry.runs"
        "  WHERE company = :'company' AND created_at < :'fence'),"
        "'filed_tickets', (SELECT count(*) FROM registry.filed_tickets"
        "  WHERE repo = :'repo' AND filed_at < :'fence'),"
        "'budget_ledger', (SELECT count(*) FROM registry.budget_ledger"
        "  WHERE company = :'company' AND ts < :'fence'),"
        "'audit_events', (SELECT count(*) FROM registry.audit_events"
        "  WHERE (company = :'company' OR run_id = :'heartbeat') AND ts < :'fence'))",
        company=COMPANY,
        repo=harness_repo_or_fail(),
        heartbeat=heartbeat_run_id(COMPANY),
        fence=fence,
    )
    assert json.loads(older) == dict.fromkeys(
        ("runs", "filed_tickets", "budget_ledger", "audit_events"), 0
    )
