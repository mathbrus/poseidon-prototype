"""Fixtures for the kind e2e suite — and the suite's contract with its
environment.

These tests run against the kind cluster brought up by
`deploy/kind/kind-up.sh`, with `deploy/kind/.env` filled with a real
GIT_TOKEN and a disposable TARGET_REPO. They open real pull requests on
that repo. Runs go through Temporal: the suite starts workflows as a thin
client (exactly what `poseidon run` does), and the in-cluster orchestrator
worker drives the activities. Cluster access lives in `cluster.py`,
blueprint application in `blueprints.py`.

Run explicitly (excluded from plain `pytest`):

    uv run pytest tests/e2e_kind -m e2e_kind

The suite holds itself to the same discipline the product has — declared
state, enforced stops, verified quiet — via four session-level guarantees:

- **The brain of record** (`paid_brain_guard`, autouse): the session fails
  before the first dispatch unless the deployed `llm-proxy` image is the
  mock, or `POSEIDON_ALLOW_PAID=1` opts into spending. What the suite
  believes about the brain comes from the deployment, never from `.env`'s
  `REAL_LLM` claim — `kind-up.sh` deliberately lets an invocation-time
  `REAL_LLM=0` beat the file. Every timer follows the same fact
  (`deployed_timeouts`): the fast profile for the mock, the workflow
  defaults for a paid brain.
- **The repo-state contract** (`repo_baseline`, autouse): TARGET_REPO@main
  is suite-managed state with one owner. The baseline seeds the declared
  shape from the mock brain's own canonical texts and prunes what it
  refuses to inherit (an implementer-owned test tree beside the contract
  tree; root pytest.ini or conftest.py, whose merged form would neuter
  every later run's collection). Idempotent — a repo already at baseline
  gets reads, not commits. No module may depend on what a previous run
  left behind.
- **A clean campsite** (`close_pr`, `sweep_repo_debris`, `repo_sweeper`,
  `blueprints.quiesce_heartbeat`): debris feeds contention — the ensure
  loop's bare heartbeat treats any open PR or issue as reviewable backlog,
  and one parked review holds `max_concurrent_runs`' whole slot for a day.
  So a fixture closes the PRs it opened the moment their openness has been
  verified; a module that applies a heartbeat-declaring blueprint sweeps
  BEFORE the apply, waits for the bare heartbeat's first tick on that
  empty backlog (`apply_profile` — the ensure loop starts it a minute or
  two after the apply, and until it has ticked once and gone to sleep it
  can steal the module's own ticket) and restores the pristine blueprint
  after; the session sweeper catches whatever a failure path dropped.
- **Quiet, asserted** (`test_quiet`, ordered last): when the suite exits
  green, no workflow is running and no run is non-terminal — a failing
  assertion, not a person asking.

What a new module owes: declare repo preconditions against `repo_baseline`
(and put deviations back); close every PR it opens and delete the branch
(`close_pr`); if it applies another blueprint profile, sweep before the
apply and `blueprints.restore_pristine()` after; and anchor assertions on
runs and gate events — attributable whichever heartbeat acted
(`heartbeat_ticks`) — never on winning a race against the ensure loop.

Collection order is the hook's, not the filenames': the capstones run
after every module whose runs they read, and `test_quiet` runs last.
"""

import asyncio
import base64
import contextlib
import functools
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from temporalio.client import Client, WorkflowExecutionStatus

from common.ids import new_run_id
from mockllm.brain import (
    CONTRACT_TEST_PATH,
    CONTRACT_TESTS_PY,
    SLUGIFY_PY,
    TEST_SLUGIFY_PY,
)
from orchestrator.workflows.base import FAST_TIMEOUTS, Timeouts, workflow_id
from orchestrator.workflows.implement import ImplementInput

from .blueprints import apply_blueprint, wait_for_convergence
from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    audit_story,
    db_now,
    kill_worker_pod,
    run_kubectl,
    sandbox_pods,
    temporal_port_forward,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "deploy" / "kind" / ".env"
TASK_QUEUE = "poseidon-orchestrator"

E2E_KIND_DIR = Path(__file__).resolve().parent

# The modules that must run after everything else, in this order: the
# acceptance capstone reads what the whole suite produced; the two
# autonomy capstones add runs of their own; the quiet check comes last.
ORDERED_LAST = [
    "test_acceptance.py",
    "test_overnight.py",
    "test_delegated.py",
    "test_quiet.py",
]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook sees the whole session's items; only mark and order this
    # suite's. The sort is stable, so everything not listed keeps pytest's
    # own order.
    for item in items:
        if item.path.is_relative_to(E2E_KIND_DIR):
            item.add_marker(pytest.mark.e2e_kind)

    def rank(item: pytest.Item) -> int:
        if not item.path.is_relative_to(E2E_KIND_DIR):
            return 0
        return (
            ORDERED_LAST.index(item.path.name) + 1
            if item.path.name in ORDERED_LAST
            else 0
        )

    items.sort(key=rank)


@pytest.fixture(scope="session", autouse=True)
def db_session_start() -> str:
    """The registry database's own clock at the start of the pytest
    session — the acceptance capstone's fence for "what this suite
    execution really produced". Autouse so it is stamped before the first
    test, whichever that is."""
    return db_now()


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        pytest.fail(f"the kind e2e suite needs a filled {ENV_FILE} (copy .env.example)")
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def stack(env: dict[str, str]) -> None:
    """Fail fast (with a useful message) when the kind stack is not up."""
    hint = "is the cluster up? deploy/kind/kind-up.sh"
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    try:
        resp = httpx.get(gateway_url + "/mcp", timeout=5)
        if resp.status_code != 401:
            pytest.fail(
                f"gateway at {gateway_url} answered "
                f"{resp.status_code}, expected 401 without a token"
            )
    except httpx.TransportError as exc:
        pytest.fail(f"cannot reach the stack ({exc}); {hint}")
    ready = run_kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "deployment/orchestrator-worker",
        "-o",
        "jsonpath={.status.readyReplicas}",
        timeout=30,
    )
    if ready.returncode != 0 or ready.stdout.strip() != "1":
        pytest.fail(f"orchestrator worker not ready; {hint}")


# --- the brain of record -----------------------------------------------------


@functools.lru_cache(maxsize=1)
def deployed_brain_image() -> str:
    """The LLM proxy image the cluster actually runs — the fact `.env`'s
    `REAL_LLM` flag merely claims. `kind-up.sh` deliberately lets an
    invocation-time `REAL_LLM=0` beat the file, so a mock cluster under a
    `REAL_LLM=1` `.env` is a normal, supported state; the deployment is the
    one source that cannot describe a brain that is not there. Cached for
    the session: the deployment does not change under a running suite.
    Empty when the cluster cannot answer."""
    proc = run_kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "deployment/llm-proxy",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
        timeout=30,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def paid_brain_problem(image: str, allow: str | None) -> str | None:
    """Why the session must not run, or None. This suite dispatches real
    LLM calls; against a paid brain those bill a provider, which is a spend
    decision no test run may make implicitly. Pure, so the refusal logic
    is pinned without a cluster. Fail-safe defaults: an unreadable brain
    refuses too — the dangerous condition is the absent input."""
    if not image:
        return (
            f"cannot read the deployed brain (deployment/llm-proxy in {PLATFORM_NS});"
            " refusing to run against an unverified brain —"
            " is the cluster up? deploy/kind/kind-up.sh"
        )
    if "mockllm" in image:
        return None
    if allow == "1":
        return None
    return (
        f"the cluster runs a PAID brain ({image}): e2e runs would bill a provider."
        " Redeploy on the mock (REAL_LLM=0 deploy/kind/kind-up.sh), or set"
        " POSEIDON_ALLOW_PAID=1 to spend deliberately."
    )


@pytest.fixture(scope="session", autouse=True)
def paid_brain_guard() -> None:
    """The LLM-cost rule, enforced rather than asked nicely: the session
    fails before the first dispatch unless the deployed brain is the mock
    or the operator opted into spending with POSEIDON_ALLOW_PAID=1."""
    problem = paid_brain_problem(
        deployed_brain_image(), os.environ.get("POSEIDON_ALLOW_PAID")
    )
    if problem:
        pytest.fail(problem)


def deployed_timeouts() -> Timeouts | None:
    """The timeout profile for the brain the cluster is *running* — every
    workflow input in the suite sizes from this, never from `.env`.

    Sizing depends on the brain: the mock answers in milliseconds, a paid
    model legitimately takes minutes, and the default profile's gate
    deadlines are hours (`budget_gate_s` 3600, `merge_gate_s` 86400).
    Those are not upper bounds a fast run slips under — the park tests
    *wait them out* — so reading the wrong brain does not merely slow the
    suite, it wedges it for a day on the first unanswered gate. None means
    the workflow defaults: the patience a paid brain deserves."""
    return FAST_TIMEOUTS if "mockllm" in deployed_brain_image() else None


# --- running implement -------------------------------------------------------


async def _execute_implement(
    address: str, input: ImplementInput, kill_worker: bool
) -> dict:
    """Start implement exactly as `poseidon run` does; optionally kill the
    worker once the sandbox exists (the crash-resume proof), then await
    the result."""
    client = await Client.connect(address)
    handle = await client.start_workflow(
        "implement",
        input,
        id=workflow_id("implement", input.run_id),
        task_queue=TASK_QUEUE,
    )
    if kill_worker:
        deadline = time.monotonic() + 300
        while not sandbox_pods(input.run_id):
            if time.monotonic() > deadline:
                raise AssertionError("sandbox never appeared; cannot stage worker kill")
            await asyncio.sleep(2)
        kill_worker_pod()
    return await asyncio.wait_for(handle.result(), timeout=1800)


def _run(input: ImplementInput, kill_worker: bool = False) -> dict:
    with temporal_port_forward() as address:
        record = asyncio.run(_execute_implement(address, input, kill_worker))
    assert record["run_id"] == input.run_id
    return record


def run_implement(task_file: str, kill_worker: bool, env: dict[str, str]) -> dict:
    """One full implement run of an examples/ task through Temporal. A nonce
    pins the delivery branch so reruns against the same disposable repo
    never collide on an existing branch or open PR. Timeouts follow the
    brain the cluster was actually deployed with (`deployed_timeouts`)."""
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / task_file).read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    return _run(
        ImplementInput(
            run_id=new_run_id(), task_text=task_text, timeouts=deployed_timeouts()
        ),
        kill_worker,
    )


def run_implement_from_issue(issue_number: int, env: dict[str, str]) -> dict:
    """One full implement run from a GitHub issue the company filed —
    what `poseidon run <number>` starts. The issue body must carry the
    delivery-branch line itself; the harness appends nothing."""
    return _run(
        ImplementInput(
            run_id=new_run_id(), issue_number=issue_number, timeouts=deployed_timeouts()
        )
    )


def heartbeat_ticks(heartbeat_id: str) -> list[dict[str, Any]]:
    """The tick rows this exact heartbeat wrote. Every heartbeat of a
    company journals under the shared `heartbeat-{company}` run id, but
    each tick row carries the writing workflow's own id in the `workflow`
    column — so this is attributable to one heartbeat even while the ensure
    loop runs a second one beside it. Assert on these, never on
    `audit_story(<your heartbeat id>)`: the decide/tick ledger is keyed by
    the company, and winning a race is not a claim."""
    return [
        e
        for e in audit_story(workflow_id("heartbeat", COMPANY_NS))
        if e["tool"] == "heartbeat.tick" and e["workflow"] == heartbeat_id
    ]


# --- the repo-state contract ------------------------------------------------
#
# TARGET_REPO@main is suite-managed state with one owner: this baseline.
# Without it a module's green would depend on residue — a contract tree
# an *earlier run's* module happened to leave behind — and a fresh
# disposable repo would carry no contract tree at all. Modules that
# need a different shape declare the deviation against this baseline and
# put it back (the contract-first capstone deletes the tree its arc
# re-merges); nobody depends on what a previous run left.


def github_api(env: dict, method: str, path: str, **kwargs) -> httpx.Response:
    return httpx.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {env['GIT_TOKEN']}",
        },
        timeout=30,
        **kwargs,
    )


def target_slug(env: dict) -> str:
    return "/".join(env["TARGET_REPO"].split("/")[-2:])


def read_repo_file(env: dict, path: str) -> dict | None:
    """The file's API record at `main` (sha included, for a write), or None
    when it is absent."""
    resp = github_api(env, "GET", f"/repos/{target_slug(env)}/contents/{path}")
    return resp.json() if resp.status_code == 200 else None


def put_repo_file(env: dict, path: str, content: str, message: str) -> None:
    """Idempotent write at `main`: a file already carrying exactly this
    content is left alone, so re-establishing the baseline on a clean repo
    makes no commits."""
    existing = read_repo_file(env, path)
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
    }
    if existing:
        if base64.b64decode(existing["content"]).decode() == content:
            return
        body["sha"] = existing["sha"]
    resp = github_api(env, "PUT", f"/repos/{target_slug(env)}/contents/{path}", json=body)
    assert resp.status_code in (200, 201), f"seeding {path} failed: {resp.text}"


def delete_repo_file(env: dict, path: str, message: str) -> None:
    existing = read_repo_file(env, path)
    if not existing:
        return
    resp = github_api(
        env,
        "DELETE",
        f"/repos/{target_slug(env)}/contents/{path}",
        json={"message": message, "sha": existing["sha"]},
    )
    assert resp.status_code == 200, f"removing {path} failed: {resp.text}"


# The declared shape of main: the mock brain's own canonical texts, so the
# baseline is exactly what the company itself delivers — the implementation,
# the implementer's unit tree, and the human-merged acceptance contract the
# composed contract verify replays against every PR head.
REPO_BASELINE: dict[str, str] = {
    "slugify.py": SLUGIFY_PY,
    "tests/unit/test_slugify.py": TEST_SLUGIFY_PY,
    CONTRACT_TEST_PATH: CONTRACT_TESTS_PY,
}

# Shapes the baseline refuses to inherit: an implementer-owned test tree
# beside the contract tree (at a path no laundering veil ignores, it breaks
# head suites for the wrong reason) and root-level collection config (a
# merged pytest.ini/conftest.py would neuter every later run's suite the
# way the red-team's veils neuter one PR's).
REPO_PRUNED: tuple[str, ...] = (
    "tests/test_slugify.py",
    "tests/test_gate.py",
    "pytest.ini",
    "conftest.py",
    "gate-counter.txt",
)


@pytest.fixture(scope="session", autouse=True)
def repo_baseline(env: dict[str, str]) -> dict[str, str]:
    """Establish the declared shape of TARGET_REPO@main once per session —
    autouse, so a standalone module run gets the same contract the full
    suite does and a fresh disposable repo needs no manual seeding.
    Idempotent: a repo already at baseline gets reads, not commits."""
    for path, content in REPO_BASELINE.items():
        put_repo_file(env, path, content, f"suite baseline: {path}")
    for path in REPO_PRUNED:
        delete_repo_file(env, path, f"suite baseline: prune {path}")
    return env


# --- leave the campsite clean ------------------------------------------------
#
# Debris feeds contention: the ensure loop's bare heartbeat treats any open
# PR as reviewable backlog, so PRs a module leaves behind become another
# module's race. Fixtures close the PRs they opened the moment they are
# done with them; the session sweeper catches what inline tests and failure
# paths drop; and modules that applied a heartbeat-declaring blueprint
# terminate the bare heartbeat their apply caused, instead of leaving it
# asleep for up to reconcile_minutes after the pristine restore.


def pr_number(pr_url: str | None) -> int | None:
    match = re.search(r"/pull/(\d+)$", pr_url or "")
    return int(match.group(1)) if match else None


def close_pr(env: dict, number: int) -> None:
    """Close one pull request (a no-op when it merged) and delete its head
    branch. Cleanup only — never lets an API hiccup mask the test outcome."""
    with contextlib.suppress(Exception):
        slug = target_slug(env)
        pull = github_api(env, "GET", f"/repos/{slug}/pulls/{number}").json()
        if pull.get("state") == "open":
            github_api(
                env, "PATCH", f"/repos/{slug}/pulls/{number}", json={"state": "closed"}
            )
        head = (pull.get("head") or {}).get("ref") or ""
        if head.startswith("feature/"):
            github_api(env, "DELETE", f"/repos/{slug}/git/refs/heads/{head}")


def close_pr_url(env: dict, pr_url: str | None) -> None:
    number = pr_number(pr_url)
    if number is not None:
        close_pr(env, number)


def pr_state(env: dict, pr_url: str | None) -> str:
    number = pr_number(pr_url)
    if number is None:
        return "missing"
    pull = github_api(env, "GET", f"/repos/{target_slug(env)}/pulls/{number}").json()
    return str(pull.get("state") or "missing")


def apply_profile(path: Path, env: dict) -> None:
    """Sweep, apply, converge, and let the bare heartbeat tick — the way a
    module that declares a heartbeat must arrive. An empty campsite first:
    the ensure loop's bare heartbeat ticks once the moment it starts, and
    any open PR or issue an earlier module left becomes its backlog — a
    review parked on a day-long gate that holds the one concurrency slot.
    Then the wait: the ensure loop starts the bare heartbeat on its own
    cadence, a minute or two after the apply, and its first tick lands on
    the swept backlog and sleeps `reconcile_minutes` — but a module that
    files its goal before that tick races the bare heartbeat for its own
    ticket, and the module's heartbeat loses the attribution its
    assertions anchor on (both start the run; the one-live-run index kills
    the loser's child at birth). `path` must declare a heartbeat section."""
    sweep_repo_debris(env)
    apply_blueprint(path)
    wait_for_convergence()
    await_bare_heartbeat_asleep()


def await_bare_heartbeat_asleep(deadline_s: int = 300) -> None:
    """Block until the ensure loop's `heartbeat-{company}` is running and
    asleep — it has observed the swept campsite and will not tick again
    for the blueprint's `reconcile_minutes`. Read off the workflow's own
    `state` query, on its current run only: a profile re-applied over
    itself finds the standing heartbeat already asleep, and a terminated
    one (a quiesced earlier profile) does not count until the ensure loop
    has started its successor."""
    bare = workflow_id("heartbeat", COMPANY_NS)

    async def wait() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(bare)
            deadline = time.monotonic() + deadline_s
            while time.monotonic() < deadline:
                # Not started yet, or between runs: keep waiting.
                with contextlib.suppress(Exception):
                    description = await handle.describe()
                    if (
                        description.status == WorkflowExecutionStatus.RUNNING
                        and await handle.query("state") == "sleeping"
                    ):
                        return
                await asyncio.sleep(3)
            raise AssertionError(
                f"the bare heartbeat {bare} was not asleep within {deadline_s}s"
                " of the apply"
            )

    asyncio.run(wait())


def sweep_repo_debris(env: dict) -> None:
    """Close every open PR, delete every feature/* branch and close every
    open issue — the stragglers per-fixture cleanup cannot see (PRs born
    inside test bodies, fixtures that died before their teardown). Open
    issues are backlog: a leftover would be the lowest-numbered ticket the
    next session's heartbeat picks first."""
    with contextlib.suppress(Exception):
        slug = target_slug(env)
        while prs := github_api(
            env, "GET", f"/repos/{slug}/pulls?state=open&per_page=100"
        ).json():
            for pull in prs:
                close_pr(env, pull["number"])
        while True:
            branches = github_api(
                env, "GET", f"/repos/{slug}/branches?per_page=100"
            ).json()
            stale = [b["name"] for b in branches if b["name"].startswith("feature/")]
            if not stale:
                break
            for name in stale:
                github_api(env, "DELETE", f"/repos/{slug}/git/refs/heads/{name}")
        while True:
            issues = [
                i
                for i in github_api(
                    env, "GET", f"/repos/{slug}/issues?state=open&per_page=100"
                ).json()
                if "pull_request" not in i
            ]
            if not issues:
                break
            for issue in issues:
                github_api(
                    env,
                    "PATCH",
                    f"/repos/{slug}/issues/{issue['number']}",
                    json={"state": "closed"},
                )


@pytest.fixture(scope="session", autouse=True)
def repo_sweeper(env: dict[str, str]):
    """The session's last word on repo debris: whatever ran, the disposable
    repo exits with no open PRs and no feature/* branches — so debris never
    grows across runs, and the next session's bare heartbeat finds no
    backlog to steal."""
    yield
    sweep_repo_debris(env)


# --- the shared runs ---------------------------------------------------------


@pytest.fixture(scope="session")
def implement_run(stack: None, env: dict[str, str]) -> dict:
    """The happy-path run, with the worker killed mid-run.

    The PR's openness-at-birth is verified here, then the PR is closed
    immediately: implement leaving the PR open is the product claim (it
    holds no merge or close authority), and a suite that parades open PRs
    across later modules hands the ensure loop's bare heartbeat backlog to
    review — the review parks on a day-long gate and starves whichever
    module needed the concurrency slot."""
    record = run_implement("task-happy.md", kill_worker=True, env=env)
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    close_pr_url(env, record.get("pr_url"))
    yield record


@pytest.fixture(scope="session")
def injected_run(stack: None, env: dict[str, str]) -> dict:
    """The injected task — the planted 'hotfix runbook' steers delivery
    onto `main`, the gateway denies it, the brain recovers onto feature/*.
    Openness verified at birth, then closed — see `implement_run`."""
    record = run_implement("task-injected.md", kill_worker=False, env=env)
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    close_pr_url(env, record.get("pr_url"))
    yield record


@pytest.fixture(scope="session")
def pr_info(env: dict[str, str]) -> Callable[[str], dict[str, Any]]:
    """Fetch a pull request from the GitHub API given its html URL."""

    def fetch(pr_url: str) -> dict[str, Any]:
        match = re.fullmatch(r"https://github\.com/(.+?/.+?)/pull/(\d+)", pr_url)
        assert match, f"unexpected PR URL: {pr_url}"
        resp = github_api(env, "GET", f"/repos/{match[1]}/pulls/{match[2]}")
        assert resp.status_code == 200, f"GitHub API: {resp.status_code} {resp.text}"
        return resp.json()

    return fetch
