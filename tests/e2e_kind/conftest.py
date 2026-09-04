"""Fixtures for the kind e2e suite — and the suite's contract with its
environment.

These tests run against the kind cluster brought up by
`deploy/kind/kind-up.sh`, with `deploy/kind/.env` filled with a real
GIT_TOKEN and a disposable TARGET_REPO. They open real pull requests on
that repo. Every run is the company's own: the suite files what the
standing heartbeat should find (a ticket, a strategy naming a slice),
wakes it with the platform's own signal, and reads the runs it starts —
exactly the loop the product is. Cluster access lives in `cluster.py`,
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
  shape from the mock brain's own canonical texts — the implementation,
  the unit tree, the acceptance contract, and the two company documents
  with no open slice, so the standing loop at rest observes nothing to
  scope — and prunes what it refuses to inherit (root pytest.ini or
  conftest.py, whose merged form would neuter every later run's
  collection). Idempotent — a repo already at baseline gets reads, not
  commits. No module may depend on what a previous run left behind.
- **One lane** (`run_ticket`, `run_review`, `apply_profile`,
  `sweep_repo_debris`, `repo_sweeper`): every run is the standing
  heartbeat's. A module files the ticket the loop should implement (or
  writes the strategy it should scope from), wakes the loop, and reads the
  run it started; what the loop may start on its own is declared in the
  profile the module applies (`blueprints.may_start`), never by a company
  without a heartbeat. Debris feeds contention — the loop treats any open
  PR or issue as backlog and wakes the moment any run finishes — so a
  fixture closes the PRs it opened the moment their openness has been
  verified, a module sweeps BEFORE it applies a profile and waits for the
  loop's arrival tick on the empty backlog (`apply_profile`), and a run
  that is *meant* to fail runs under `blueprints.single_attempt`, or the
  loop retries it. The session sweeper catches whatever a failure path
  dropped.
- **Quiet, asserted** (`test_quiet`, ordered last): when the suite exits
  green, no workflow but the standing heartbeat is running, it is asleep
  on an empty backlog, and no run is non-terminal — a failing assertion,
  not a person asking.

What a new module owes: declare repo preconditions against `repo_baseline`
(and put deviations back — a strategy that names a slice above all);
close every PR it opens and delete the branch (`close_pr`), and close the
tickets it filed; if it applies another blueprint profile, sweep before
the apply and `blueprints.restore_pristine()` after; bound the attempts
of a run it expects to fail; and anchor assertions on runs, gate events
and tick windows (`heartbeat_ticks` past a counted mark), never on the
ledger's whole history.

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
import yaml
from temporalio.client import Client, WorkflowExecutionStatus

from common.tasks import issue_run_task, review_run_task
from mockllm.brain import (
    ARCHITECTURE_MD,
    CONTRACT_TEST_PATH,
    CONTRACT_TESTS_PY,
    SLUGIFY_PY,
    STRATEGY_MD,
    TEST_SLUGIFY_PY,
)
from orchestrator.workflows.base import PROFILES, Timeouts, workflow_id

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    audit_story,
    await_run,
    db_now,
    deployment_env,
    kill_worker_pod,
    kubectl,
    run_by_task,
    run_kubectl,
    sandbox_pods,
    temporal_port_forward,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "deploy" / "kind" / ".env"

E2E_KIND_DIR = Path(__file__).resolve().parent
# The ticket bodies the suite files: the mock brain's scripted scenarios,
# as a company's own tickets rather than a human's task files.
TICKETS_DIR = E2E_KIND_DIR / "tickets"
# Where the company keeps its documents, as the example declares it — the
# suite writes strategies there and the baseline puts the resting ones back.
DOCUMENT_PATHS: dict[str, str] = yaml.safe_load(HELLO_SAAS.read_text())["company"][
    "documents"
]

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


def deployed_timeouts() -> Timeouts:
    """The timeout profile the worker is *deployed* with — what every
    workflow in the cluster runs under, read back so the suite sizes its
    own waits from it, never from `.env`.

    Gate waits are not in the profile: they are blueprint fields, and the
    suite applies its own (`blueprints.E2E_GATE_MINUTES`)."""
    return PROFILES[deployment_env("orchestrator-worker", "TIMEOUT_PROFILE")]


# --- runs: the heartbeat's, always ------------------------------------------
#
# The suite never starts a run-shaped workflow itself. It files what the
# loop should find, wakes it, and reads the run the loop started — the
# product's own path, with the harness standing in for the outside world.


def ticket_body(name: str, nonce: str) -> str:
    """One scripted scenario as a ticket body: the text under `tickets/`
    plus the delivery-branch line an issue-driven run parses, nonced so
    reruns against the disposable repo never collide on a branch."""
    return (TICKETS_DIR / f"{name}.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )


def file_ticket(env: dict, title: str, body: str) -> int:
    """File one issue on the disposable repo — the backlog entry the
    standing heartbeat will find — and return its number."""
    created = github_api(
        env,
        "POST",
        f"/repos/{target_slug(env)}/issues",
        json={"title": title, "body": body},
    )
    assert created.status_code == 201, f"could not file issue: {created.text}"
    return int(created.json()["number"])


def _kill_worker_when(run_id: str, kill_after: str) -> None:
    """The crash-resume proof's staging: kill the worker once the run has
    its sandbox (`"sandbox"`) or its crew's own pod (`"crew"`), so the
    replay on the replacement worker inherits live state to compensate."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if kill_after == "sandbox" and sandbox_pods(run_id):
            break
        if kill_after == "crew" and kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "pods",
            "-l",
            f"poseidon.dev/kind=ephemeral-agent,poseidon.dev/run-id={run_id}",
            "-o",
            "jsonpath={.items[*].metadata.name}",
            timeout=30,
        ):
            break
        time.sleep(2)
    else:
        raise AssertionError(
            f"the run never reached its {kill_after}; cannot stage the kill"
        )
    kill_worker_pod()


def run_ticket(
    env: dict, number: int, *, kill_after: str | None = None, deadline_s: int = 1800
) -> dict:
    """The implement run the standing heartbeat starts on ticket `number`:
    wake the loop onto it (the platform's own signal, standing in for the
    world the loop watches), optionally kill the worker mid-run, and wait
    for the run's terminal registry row — the record every other helper
    and assertion reads. `implement` must be startable in the applied
    profile."""
    task = issue_run_task(number)
    wake_until_run_started(task, "implement")
    if kill_after:
        run = run_by_task(task, workflow="implement")
        _kill_worker_when(run["run_id"], kill_after)
    record = await_run(task, terminal=True, deadline_s=deadline_s)
    return {**record, "issue_number": number}


def run_implement(name: str, env: dict, *, kill_after: str | None = None) -> dict:
    """File the scripted ticket `tickets/<name>.md` and let the heartbeat
    implement it. The ticket number rides the record for cleanup."""
    nonce = secrets.token_hex(3)
    number = file_ticket(env, f"{name} ({nonce})", ticket_body(name, nonce))
    return run_ticket(env, number, kill_after=kill_after)


def run_review(
    env: dict, pr_number: int, approve: bool | None = None, deadline_s: int = 900
) -> tuple[str, dict]:
    """The review run the standing heartbeat starts on the delivered PR
    `pr_number` (the run that delivered it wakes the loop; the wake here
    covers a backlog read that landed too early), driven to a terminal
    state. With `approve` None, no human touches the `merge` signal — an
    in-class PR merges under policy on its own, a declined one parks,
    escalates and expires under the suite's gate clock. With `approve` a
    bool, wait for the merge-gate park and deliver that one human verdict.
    Returns the review run id and its terminal record. `review` must be
    startable in the applied profile."""
    task = review_run_task(pr_number)
    wake_until_run_started(task, "review")
    run_id = run_by_task(task, workflow="review")["run_id"]
    if approve is not None:

        async def answer() -> None:
            with temporal_port_forward() as address:
                client = await Client.connect(address)
                handle = client.get_workflow_handle(workflow_id("review", run_id))
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    if await handle.query("state") == "paused-merge_gate":
                        break
                    await asyncio.sleep(0.5)
                else:
                    raise AssertionError(f"review of #{pr_number} never parked on merge")
                await handle.signal("merge", args=[approve, "e2e: human verdict"])

        asyncio.run(answer())
    return run_id, await_run(task, terminal=True, deadline_s=deadline_s)


def write_strategy(env: dict, text: str) -> None:
    """A declared deviation from the baseline: the strategy the loop will
    read on its next tick. The mock PM scopes a slice only when it names
    one; put the resting strategy back (`restore_strategy`) before the
    module ends, or the loop keeps scoping."""
    put_repo_file(env, DOCUMENT_PATHS["strategy"], text, "e2e: strategy names a slice")


def restore_strategy(env: dict) -> None:
    put_repo_file(env, DOCUMENT_PATHS["strategy"], STRATEGY_MD, "e2e: strategy at rest")


def open_slice_strategy(nonce: str, marker: str) -> str:
    """A strategy naming one slice for the PM to scope, with the delivery
    branch the ticket — and so the implement run — will carry."""
    return (
        "# Strategy\n\n"
        f"Next slice: ship the slugify utility ({marker}; e2e {nonce}).\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.\n"
    )


def heartbeat_ticks(heartbeat_id: str) -> list[dict[str, Any]]:
    """The tick rows one heartbeat workflow wrote — the ledger journals
    every tick under the shared `heartbeat-{company}` run id, and each row
    carries the writing workflow's own id in the `workflow` column. The
    standing heartbeat keeps its id across restarts, so this list grows
    for as long as the session runs: scope assertions to a window (count
    the rows, act, assert on the rows past the mark) or to rules that
    name a run, never to the whole history."""
    return [
        e
        for e in audit_story(workflow_id("heartbeat", COMPANY_NS))
        if e["tool"] == "heartbeat.tick" and e["workflow"] == heartbeat_id
    ]


def bare_heartbeat_id() -> str:
    """The standing heartbeat's workflow id — the one the ensure loop
    keeps running and the platform's own wakes reach."""
    return workflow_id("heartbeat", COMPANY_NS)


def wake_bare_heartbeat() -> None:
    """Send the platform's own `wake` signal to the standing heartbeat —
    the harness's stand-in for a change only the outside world saw (an
    issue filed by hand, a PR merged by hand), compressed from the
    reconcile wait to now. A real capability exercised, not a seam:
    production sends the same signal."""

    async def send() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            await client.get_workflow_handle(bare_heartbeat_id()).signal("wake")

    asyncio.run(send())


def await_new_ticks(mark: int, n: int, deadline_s: int = 180) -> list[dict[str, Any]]:
    """Block until the standing heartbeat has journaled at least `mark + n`
    tick rows and return the rows past the mark. Count-then-wake
    discipline: take the mark, wake once, wait here before waking again —
    a wake landing mid-tick coalesces, so blind sends under-count."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        ticks = heartbeat_ticks(bare_heartbeat_id())
        if len(ticks) >= mark + n:
            return ticks[mark:]
        time.sleep(3)
    raise AssertionError(
        f"the heartbeat wrote {len(heartbeat_ticks(bare_heartbeat_id()))} ticks"
        f" within {deadline_s}s; needed {mark + n}"
    )


def wake_until_run_started(task: str, workflow: str, deadline_s: int = 600) -> None:
    """Nudge the standing heartbeat until the run it should start exists.
    One wake usually does it; the re-nudge covers a tick whose backlog
    read landed before the just-filed issue or just-delivered PR was
    visible on the git host — the extra ticks it may cause are idle rows,
    never actions."""
    deadline = time.monotonic() + deadline_s
    next_wake = 0.0
    while time.monotonic() < deadline:
        if run_by_task(task, workflow=workflow) is not None:
            return
        if time.monotonic() >= next_wake:
            wake_bare_heartbeat()
            next_wake = time.monotonic() + 60
        time.sleep(3)
    raise AssertionError(f"the heartbeat never started {task!r}")


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
# the implementer's unit tree, the human-merged acceptance contract the
# composed contract verify replays against every PR head, and the two
# documents the company wrote for itself.
REPO_BASELINE: dict[str, str] = {
    "slugify.py": SLUGIFY_PY,
    "tests/unit/test_slugify.py": TEST_SLUGIFY_PY,
    CONTRACT_TEST_PATH: CONTRACT_TESTS_PY,
    # The company's documents at rest: neither names an open slice, so
    # the standing loop observes nothing to scope between modules.
    DOCUMENT_PATHS["strategy"]: STRATEGY_MD,
    DOCUMENT_PATHS["architecture"]: ARCHITECTURE_MD,
}

# Shapes the baseline refuses to inherit: root-level collection config (a
# merged pytest.ini/conftest.py would neuter every later run's suite the
# way the red-team's veils neuter one PR's) and stray gate-counter state a
# run that died mid-arc may have left on main.
REPO_PRUNED: tuple[str, ...] = (
    "pytest.ini",
    "conftest.py",
    "gate-counter.txt",
)


@pytest.fixture(scope="session", autouse=True)
def suite_blueprint(stack: None) -> None:
    """The company the suite runs against: the pristine example on the
    suite's clock (`blueprints.suite_document`). The bootstrap applied the
    example as written, whose gates wait a working day; the suite applies
    it again with its own waits before the first test, so a module that
    parks on a gate watches it escalate within a minute."""
    restore_pristine()


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
# Debris feeds contention: the standing heartbeat treats any open PR or
# issue as backlog — and it wakes whenever a run finishes — so what a
# module leaves behind becomes another module's race. Fixtures close the
# PRs and tickets they made the moment they are done with them; the
# session sweeper catches what inline tests and failure paths drop.


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


def close_issue(env: dict, number: int | None) -> None:
    """Close one issue by number. Cleanup only — suppressed so an API
    hiccup never masks the test outcome."""
    if number is None:
        return
    with contextlib.suppress(Exception):
        github_api(
            env,
            "PATCH",
            f"/repos/{target_slug(env)}/issues/{number}",
            json={"state": "closed"},
        )


def gate_merge_rules(story: list[dict]) -> list[str]:
    """The merge-gate rules a run's audit story recorded, in order."""
    return [e["rule"] for e in story if e["tool"] == "gate.merge"]


def pr_state(env: dict, pr_url: str | None) -> str:
    number = pr_number(pr_url)
    if number is None:
        return "missing"
    pull = github_api(env, "GET", f"/repos/{target_slug(env)}/pulls/{number}").json()
    return str(pull.get("state") or "missing")


def apply_profile(path: Path, env: dict) -> None:
    """Sweep, apply, converge, and let the standing heartbeat settle — the
    way every module that applies its own profile must start. An empty
    campsite first: any open PR or issue an earlier module left becomes the
    loop's backlog the moment it reads the new section — a review parked
    on the gate that holds the one concurrency slot. Then the wait: once
    the loop is parked on its wait over the swept backlog, every tick that
    follows is one the module caused — its own runs wake the heartbeat
    forward, and `wake_bare_heartbeat` stands in for the outside world."""
    sweep_repo_debris(env)
    apply_blueprint(path)
    wait_for_convergence()
    await_bare_heartbeat_asleep()


def await_bare_heartbeat_asleep(deadline_s: int = 300) -> None:
    """Block until the company's `heartbeat-{company}` is running and
    parked on its wait, so tick counts taken after this are the module's
    own. Read off the workflow's own `state` query: a profile re-applied
    over itself finds the standing heartbeat already waiting. The parked
    loop answers a wake immediately — "sleeping" means ready, never
    dormant."""
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
    repo exits with no open PRs, no feature/* branches and no open issues
    — so debris never grows across runs, and the next session's standing
    heartbeat finds no backlog to steal. Then the loop is given its idle
    tick on that empty backlog, so `test_quiet`'s successor session starts
    from rest."""
    yield
    sweep_repo_debris(env)
    with contextlib.suppress(Exception):
        wake_bare_heartbeat()
        await_bare_heartbeat_asleep()


# --- the shared runs ---------------------------------------------------------


@pytest.fixture(scope="session")
def implement_run(stack: None, env: dict[str, str]) -> dict:
    """The happy-path run, with the worker killed mid-run: the company's
    own implement run on the ticket the suite filed.

    The PR's openness-at-birth is verified here, then the PR and the
    ticket are closed immediately: implement leaving the PR open is the
    product claim (it holds no merge or close authority), and a suite that
    parades open PRs across later modules hands the standing heartbeat
    backlog — a review parked on the gate that starves whichever module
    needed the concurrency slot."""
    record = run_implement("happy", env, kill_after="sandbox")
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    close_pr_url(env, record.get("pr_url"))
    close_issue(env, record["issue_number"])
    yield record


@pytest.fixture(scope="session")
def injected_run(stack: None, env: dict[str, str]) -> dict:
    """The injected ticket — the planted 'hotfix runbook' steers delivery
    onto `main`, the gateway denies it, the brain recovers onto feature/*.
    Openness verified at birth, then closed — see `implement_run`."""
    record = run_implement("injected", env)
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    close_pr_url(env, record.get("pr_url"))
    close_issue(env, record["issue_number"])
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
