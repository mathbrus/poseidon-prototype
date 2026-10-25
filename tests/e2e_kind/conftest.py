"""Fixtures for the kind e2e suite — and the suite's contract with its
environment.

These tests run against the kind cluster brought up by
`deploy/kind/kind-up.sh` — the platform alone; the suite applies its own
company, `hello-saas`, and the cluster may host nothing or that name —
with `deploy/kind/.env` filled with a real GIT_TOKEN and the console's
address, and `E2E_TARGET_REPO` in the environment naming a disposable
repository. They open real pull requests on that repo. Every run is the company's own: the suite
files what the standing heartbeat should find (a ticket, a strategy naming
a slice), wakes it, and reads the runs it starts — exactly the loop the
product is. Every *act* on the company is the console's, as the human the
chart declares (`owner`, with a token minted like `poseidon login`); what
the suite *observes* it reads with its own instruments — psql, kubectl,
the git host — never the product's word for it. Cluster access lives in
`cluster.py`, blueprint application in `blueprints.py`.

Run explicitly (excluded from plain `pytest`):

    uv run pytest tests/e2e_kind -m e2e_kind

The suite holds itself to the same discipline the product has — declared
state, enforced stops, verified quiet — via five session-level guarantees:

- **The brain of record** (`paid_brain_guard`, autouse): the session fails
  before the first dispatch unless the deployed `llm-proxy` image is the
  mock, or `POSEIDON_ALLOW_PAID=1` opts into spending. What the suite
  believes about the brain comes from the deployment, never from `.env`'s
  `REAL_LLM` claim — `kind-up.sh` deliberately lets an invocation-time
  `REAL_LLM=0` beat the file. The suite's waits are fixed wall-clock
  budgets sized for the mock; a paid brain is an opt-in the waits are not
  sized for.
- **The repo-state contract** (`baseline_env`, autouse): the disposable
  repository's main branch (`E2E_TARGET_REPO`) is suite-managed state with
  one owner. The baseline seeds the declared
  shape from the mock brain's own canonical texts — the implementation,
  the unit tree, the acceptance contract, and the two company documents
  with no open slice, so the standing loop at rest observes nothing to
  scope — and prunes what it refuses to inherit (root pytest.ini or
  conftest.py, whose merged form would neuter every later run's
  collection). Idempotent — a repo already at baseline gets reads, not
  commits. No module may depend on what a previous run left behind.
- **One lane** (`run_ticket`, `run_review`, `apply_and_settle`,
  `sweep_repo_debris`, `repo_sweeper`): every run is the heartbeat's. A
  module files the ticket the loop should implement (or writes the
  strategy it should scope from), wakes the loop, and reads the run it
  started; what the loop may start on its own is declared in the derived
  blueprint the module applies (`blueprints.may_start`), never by a
  company without a heartbeat. Debris feeds contention — the loop treats
  any open PR or issue as backlog and wakes the moment any run finishes —
  so a module closes a PR as soon as its claims about it are read, or
  holds it open only under a blueprint the loop cannot start a review
  on; a module sweeps BEFORE it applies a blueprint and waits for the
  loop to be asleep on the swept backlog (`apply_and_settle`), so the
  ticks it counts from there are its own; and a run that is *meant* to
  fail runs under `blueprints.single_attempt`, or the loop retries it. A
  module that pauses the heartbeat resumes it before it ends — a paused
  loop never reports asleep, so the next apply would wait forever. The
  session sweeper catches whatever a failure path dropped, a pause
  included.
- **Read your own writes** (`file_ticket`, `scope_via_heartbeat`,
  `run_ticket`): the git host lists a new issue up to a minute after
  creating it, and the loop reads that listing. The platform reads the
  tickets it filed itself from the registry; the harness, standing in
  for the world, files a ticket only once the host lists it, and looks
  a run up by its task with a since mark where the task text is shared
  (every scope run's). No module reads a listing it just wrote to.
- **Quiet, asserted** (`test_quiet`, ordered last): when the suite exits
  green, no workflow but the standing heartbeat is running, it is asleep
  on an empty backlog, and no run is non-terminal — a failing assertion,
  not a person asking.

What a new module owes: declare repo preconditions against `baseline_env`
(and put deviations back — a strategy that names a slice above all);
close every PR it opens and delete the branch (`close_pr`), and close the
tickets it filed; if it applies a derived blueprint, sweep before
the apply and `blueprints.restore_pristine()` after; bound the attempts
of a run it expects to fail; act through the console (`cluster.console`),
never by a raw Temporal signal — except a red-team proving the raw road
releases nothing; and anchor assertions on runs, gate events and tick
windows (`cluster.wake_and_await_ticks`), never on the ledger's whole
history.

Collection order is the hook's, not the filenames': the capstones run
after every module whose runs they read, the delete after the capstones
(it erases what they read), and `test_quiet` runs last.
"""

import base64
import contextlib
import functools
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from common.audit import gate_event
from common.repos import repo_slug
from common.tasks import SCOPE_TASK, issue_run_task, review_run_task
from common.workflows import MERGE_GATE
from mock_brain.brain import (
    ARCHITECTURE_MD,
    CONTRACT_TEST_PATH,
    CONTRACT_TESTS_PY,
    SLUGIFY_PY,
    STRATEGY_MD,
    TEST_SLUGIFY_PY,
)

from ..brain_guard import paid_brain_problem
from .blueprints import (
    E2E_GATE_MINUTES,
    ensure_applied,
    restore_pristine,
)
from .cluster import (
    COMPANY_NS,
    HELLO_SAAS,
    PLATFORM_NS,
    REPO_ROOT,
    REWAKE_S,
    STEP_TIMES,
    await_heartbeat_asleep,
    await_run,
    console,
    console_url,
    db_now,
    harness_repo_or_fail,
    kill_worker_pod,
    kubectl,
    run_by_task,
    run_kubectl,
    timed,
    wake_heartbeat,
)

ENV_FILE = REPO_ROOT / "deploy" / "kind" / ".env"

E2E_KIND_DIR = Path(__file__).resolve().parent
# The ticket bodies the suite files: the mock brain's scripted scenarios,
# as the world files them on the company's repository.
TICKETS_DIR = E2E_KIND_DIR / "tickets"
# Where the company keeps its documents, as the example declares it — the
# suite writes strategies there and the baseline puts the resting ones back.
DOCUMENT_PATHS: dict[str, str] = yaml.safe_load(HELLO_SAAS.read_text())["company"][
    "documents"
]

# The modules that must run after everything else, in this order: the
# acceptance capstone reads what the whole suite produced; the autonomy
# capstone adds runs of its own; the delete erases every row and history
# the session made, so nothing after it may read them; the quiet check
# comes last.
ORDERED_LAST = [
    "test_acceptance.py",
    "test_automated.py",
    "test_delete.py",
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


def pytest_terminal_summary(terminalreporter) -> None:
    """Where the harness's own waits went this session, per step: how
    often, in all, and at most."""
    if not STEP_TIMES:
        return
    terminalreporter.section("harness waits")
    for step, times in sorted(STEP_TIMES.items(), key=lambda kv: -sum(kv[1])):
        terminalreporter.write_line(
            f"{step:<34} {len(times):>4}×  {sum(times):8.1f} s in all"
            f"  {max(times):6.1f} s at most"
        )


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
    # The repository is the suite's, not the deployment's: it comes from
    # the suite's own variable, never from the file.
    values["TARGET_REPO"] = harness_repo_or_fail()
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
        resp = httpx.get(console_url(env) + "/whoami", timeout=5)
        if resp.status_code != 401:
            pytest.fail(
                f"console at {console_url(env)} answered "
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


@pytest.fixture(scope="session", autouse=True)
def paid_brain_guard() -> None:
    """The LLM-cost rule, enforced rather than asked nicely: the session
    fails before the first dispatch unless the deployed brain is the mock
    or a human opted into spending with POSEIDON_ALLOW_PAID=1."""
    problem = paid_brain_problem(
        deployed_brain_image(), os.environ.get("POSEIDON_ALLOW_PAID")
    )
    if problem:
        pytest.fail(problem)


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
    heartbeat will find — and return its number once the git host lists
    it (see `file_tickets`)."""
    (number,) = file_tickets(env, [(title, body)])
    return number


def file_tickets(env: dict, tickets: list[tuple[str, str]]) -> list[int]:
    """File each `(title, body)` and return the numbers once the git host
    lists them all. GitHub lists a new issue up to a minute after creating
    it, and the loop reads the listing: a ticket the world filed is only
    filed once the world can see it. One wait covers the batch."""
    numbers = []
    for title, body in tickets:
        created = github_api(
            env,
            "POST",
            f"/repos/{target_slug(env)}/issues",
            json={"title": title, "body": body},
        )
        assert created.status_code == 201, f"could not file issue: {created.text}"
        numbers.append(int(created.json()["number"]))
    _await_listing(env, lambda listed: set(numbers) <= listed, f"issues {numbers}")
    return numbers


def await_unlisted(env: dict, number: int) -> None:
    """Block until the git host's open listing no longer shows issue
    `number` — what the loop reads, which lags a close the way it lags a
    creation."""
    _await_listing(env, lambda listed: number not in listed, f"issue #{number} closed")


def _await_listing(env: dict, done: Callable[[set[int]], bool], what: str) -> None:
    with timed("git host listing"):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            listed = github_api(
                env, "GET", f"/repos/{target_slug(env)}/issues?state=open&per_page=100"
            ).json()
            if done({int(i["number"]) for i in listed}):
                return
            time.sleep(5)
    raise AssertionError(f"the git host's listing never showed {what}")


def _kill_worker_once_the_crew_is_up(run_id: str) -> None:
    """The crash-resume proof's staging: kill the worker once the run's
    crew has its own pod — the sandbox is up by then too — so the replay
    on the replacement worker inherits live state to compensate."""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if kubectl(
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
        raise AssertionError("the run's crew never came up; cannot stage the kill")
    kill_worker_pod()


def run_ticket(number: int, *, kill_worker: bool = False, deadline_s: int = 1800) -> dict:
    """The implement run the heartbeat starts on ticket `number`: wake the
    loop onto it (the platform's own signal, standing in for the world the
    loop watches), with `kill_worker` kill the worker mid-run, and wait
    for the run's terminal registry row — the record every other helper
    and assertion reads. `implement` must be startable in the applied
    blueprint."""
    task = issue_run_task(number)
    wake_until_run_started(task, "implement")
    if kill_worker:
        _kill_worker_once_the_crew_is_up(
            run_by_task(task, workflow="implement")["run_id"]
        )
    record = await_run(task, terminal=True, deadline_s=deadline_s, workflow="implement")
    return {**record, "issue_number": number}


def run_implement(name: str, env: dict, *, kill_worker: bool = False) -> dict:
    """File the scripted ticket `tickets/<name>.md` and let the heartbeat
    implement it. The ticket number rides the record for cleanup."""
    nonce = secrets.token_hex(3)
    number = file_ticket(env, f"{name} ({nonce})", ticket_body(name, nonce))
    return run_ticket(number, kill_worker=kill_worker)


def run_review(
    pr_number: int, approve: bool | None = None, deadline_s: int = 900
) -> tuple[str, dict]:
    """The review run the standing heartbeat starts on the delivered PR
    `pr_number` (the run that delivered it wakes the loop; the wake here
    covers a backlog read that landed too early), driven to a terminal
    state. With `approve` None, no human touches the `merge` signal — an
    automated PR merges on its own, a declined one parks,
    escalates and expires under the suite's gate clock. With `approve` a
    bool, wait for the merge-gate park and deliver that one human verdict.
    Returns the review run id and its terminal record. `review` must be
    startable in the applied blueprint."""
    task = review_run_task(pr_number)
    wake_until_run_started(task, "review")
    run_id = run_by_task(task, workflow="review")["run_id"]
    if approve is not None:
        await_gate(run_id, "parked-merge_gate")
        console().resolve(run_id, "merge", approve, "e2e: human verdict")
    return run_id, await_run(
        task, terminal=True, deadline_s=deadline_s, workflow="review"
    )


def await_gate(run_id: str, state: str, deadline_s: int = 600) -> None:
    """Block until the run's workflow reports the API-visible `state` a
    parked gate must show — read through the console, as a person
    deciding whether to answer would."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if console().run(run_id).execution.state == state:
            return
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {state!r}")


def write_strategy(env: dict, text: str) -> None:
    """A declared deviation from the baseline: the strategy the loop will
    read on its next tick. The mock PM scopes a slice only when it names
    one and no open ticket covers it; put the resting strategy back
    (`restore_strategy`) once it is scoped, or the loop scopes it again
    when the slice's ticket closes."""
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


def wake_until_run_started(
    task: str, workflow: str, deadline_s: int = 600, since: str | None = None
) -> None:
    """Nudge the standing heartbeat until the run it should start exists
    (born after `since` when given). One wake usually does it; the re-nudge
    covers a tick whose backlog read landed before the just-filed issue or
    just-delivered PR was visible on the git host — the extra ticks it may
    cause are idle rows, never actions."""
    with timed("wake until run started"):
        deadline = time.monotonic() + deadline_s
        next_wake = 0.0
        while time.monotonic() < deadline:
            if run_by_task(task, workflow=workflow, since=since) is not None:
                return
            if time.monotonic() >= next_wake:
                wake_heartbeat()
                next_wake = time.monotonic() + REWAKE_S
            time.sleep(3)
    raise AssertionError(f"the heartbeat never started {task!r}")


def scope_via_heartbeat(env: dict, strategy: str, deadline_s: int = 600) -> dict:
    """Write the strategy, wake the loop onto it, and return the terminal
    record of the scope run it starts. Every scope run works the same task
    text, so the run is the one born after this write — never an earlier
    module's.

    The strategy is a one-shot input: once the scope run is over, the
    tickets carry the slice and the resting strategy goes back. Left
    naming the slice, it would be scoped again the moment the slice's
    ticket closes with its merged pull request — the platform closes it —
    and the module's arc would grow a second one nobody asked for."""
    since = db_now()
    write_strategy(env, strategy)
    try:
        wake_until_run_started(SCOPE_TASK, "scope", since=since)
        return await_run(
            SCOPE_TASK,
            terminal=True,
            deadline_s=deadline_s,
            workflow="scope",
            since=since,
        )
    finally:
        restore_strategy(env)


# --- the repo-state contract ------------------------------------------------
#
# The disposable repository's main is suite-managed state with one owner: this baseline.
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
    return repo_slug(env["TARGET_REPO"])


def read_repo_file(env: dict, path: str) -> dict | None:
    """The file's API record at `main` (sha included, for a write), or None
    when it is absent. Only a 404 means absent: a throttled or failing
    read must not be taken for an empty path and answered with a write."""
    for attempt in range(5):
        resp = github_api(env, "GET", f"/repos/{target_slug(env)}/contents/{path}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        time.sleep(2 * (attempt + 1))
    raise AssertionError(f"reading {path} failed: {resp.status_code} {resp.text}")


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


def put_branch_file(env: dict, branch: str, path: str, content: str, message: str) -> str:
    """One commit on `branch` writing `path`, pushed with the suite's token
    — the world moving a branch under the platform. Returns the commit."""
    existing = github_api(
        env, "GET", f"/repos/{target_slug(env)}/contents/{path}?ref={branch}"
    )
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]
    resp = github_api(env, "PUT", f"/repos/{target_slug(env)}/contents/{path}", json=body)
    assert resp.status_code in (200, 201), f"writing {path} on {branch}: {resp.text}"
    return resp.json()["commit"]["sha"]


def open_world_pr(env: dict, branch: str, files: dict[str, str], title: str) -> int:
    """A pull request the world opens — a `feature/*` branch off main
    carrying `files`, pushed and opened with the suite's token — for a
    module whose story starts at a review of work the company did not
    deliver. The sweeper deletes the branch like any other."""
    slug = target_slug(env)
    main = github_api(env, "GET", f"/repos/{slug}/git/ref/heads/main").json()
    made = github_api(
        env,
        "POST",
        f"/repos/{slug}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": main["object"]["sha"]},
    )
    assert made.status_code == 201, f"creating {branch}: {made.text}"
    for path, content in files.items():
        put_branch_file(env, branch, path, content, f"{title}: {path}")
    opened = github_api(
        env,
        "POST",
        f"/repos/{slug}/pulls",
        json={"title": title, "head": branch, "base": "main", "body": title},
    )
    assert opened.status_code == 201, f"opening the PR: {opened.text}"
    return int(opened.json()["number"])


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
def suite_blueprint(stack: None, baseline_env: dict[str, str]) -> None:
    """The company the suite runs against: the pristine blueprint on the
    suite's clock (`blueprints.suite_document`). The bootstrap applied the
    example as written, whose gates wait a working day, and its heartbeat
    has been working whatever the repository held since — a vision-only
    repository gets its bootstrap tickets — so the suite starts from rest:
    the baseline is seeded first (the fixture dependency), the campsite
    swept, every run in flight allowed to end, and only then the pristine
    blueprint applied with the suite's own waits, so a module that parks on
    a gate watches it escalate within a minute."""
    sweep_repo_debris(baseline_env)
    await_no_runs_in_flight()
    restore_pristine()


def await_no_runs_in_flight(deadline_s: int = 1200) -> None:
    """Block until the registry shows no non-terminal run — what a run the
    standing heartbeat started before the suite owes it: an end."""
    from .cluster import non_terminal_runs

    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not non_terminal_runs():
            return
        time.sleep(5)
    raise AssertionError(
        f"runs still in flight after {deadline_s}s: {non_terminal_runs()}"
    )


@pytest.fixture(scope="session", autouse=True)
def baseline_env(env: dict[str, str]) -> dict[str, str]:
    """Establish the declared shape of the disposable repository's main once per session —
    autouse, so a standalone module run gets the same contract the full
    suite does and a fresh disposable repo needs no manual seeding."""
    seed_baseline(env)
    return env


def seed_baseline(env: dict[str, str]) -> None:
    """Write the declared shape onto main and prune what it refuses.
    Idempotent: a repo already at baseline gets reads, not commits; an
    empty one — a purged one — gets every file."""
    for path, content in REPO_BASELINE.items():
        put_repo_file(env, path, content, f"suite baseline: {path}")
    for path in REPO_PRUNED:
        delete_repo_file(env, path, f"suite baseline: prune {path}")


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


def issue_state(env: dict, number: int) -> str:
    """An issue's state on the git host, read by number — never off the
    open listing, which may still show a just-closed ticket."""
    issue = github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{number}").json()
    return str(issue.get("state") or "missing")


def platform_closes(story: list[dict], tool: str) -> list[dict]:
    """The allowed platform-lifecycle calls of a closing tool (`issues.close`,
    `git.close_pr`) a run's audit story recorded, in order."""
    return [
        e
        for e in story
        if e["tool"] == tool and e["decision"] == "allow" and e["lifecycle"] == "platform"
    ]


def assert_merge_closed_tickets(
    env: dict, story: list[dict], pr: int, tickets: list[int]
) -> None:
    """The merge's consequence, on the host and in the ledger: every ticket
    the merged PR fulfilled reads closed, and the review run's story closed
    them under the platform identity strictly after the merge, naming it."""
    for number in tickets:
        assert issue_state(env, number) == "closed", f"ticket #{number} still open"
    closes = platform_closes(story, "issues.close")
    assert len(closes) >= len(tickets), closes
    merges = [
        i
        for i, e in enumerate(story)
        if e["tool"] == "git.merge_pr" and e["decision"] == "allow"
    ]
    assert merges and max(merges) < story.index(closes[0]), (
        "a ticket closed before the merge landed"
    )
    assert all(f"#{pr}" in (e["rationale"] or "") for e in closes), closes


def gate_merge_rules(story: list[dict]) -> list[str]:
    """The merge-gate rules a run's audit story recorded, in order."""
    return [e["rule"] for e in story if e["tool"] == gate_event(MERGE_GATE)]


def pr_state(env: dict, pr_url: str | None) -> str:
    number = pr_number(pr_url)
    if number is None:
        return "missing"
    pull = github_api(env, "GET", f"/repos/{target_slug(env)}/pulls/{number}").json()
    return str(pull.get("state") or "missing")


def apply_and_settle(path: Path, env: dict, gate_minutes: int = E2E_GATE_MINUTES) -> None:
    """Sweep, apply, converge, and wait for the heartbeat to be asleep —
    the way every module that applies its own blueprint must start. An
    apply the console already enforces is not repeated
    (`blueprints.ensure_applied`); the sweep and the wait always run. An empty campsite first: any open PR or issue an earlier module
    left becomes the loop's backlog the moment it reads the new section —
    a review the module never asked for, or a ticket it never filed. Then
    the wait: with the loop asleep over the swept backlog, the ticks a
    module counts from here are its own — its runs wake the heartbeat
    forward, and `cluster.wake_and_await_ticks` stands in for the outside
    world."""
    sweep_repo_debris(env)
    lift_pause(env)
    ensure_applied(path, gate_minutes)
    with timed("heartbeat asleep"):
        await_heartbeat_asleep()


@contextlib.contextmanager
def heartbeat_held(env: dict, rationale: str) -> Iterator[None]:
    """Hold the heartbeat for a stretch of a module — a pause through the
    console, as the owner, lifted on the way out whatever happens. A run in
    flight finishes; what it leaves behind (a delivered PR, a ticket the
    platform filed) waits for the module to read and clear it instead of
    becoming the loop's next run."""
    console(env).pause(rationale)
    try:
        yield
    finally:
        lift_pause(env)


def lift_pause(env: dict) -> None:
    """Resume a heartbeat a module left paused — through the console, as
    the owner, so the resume is on the record like any other. A loop
    nobody paused needs nothing."""
    with contextlib.suppress(Exception):
        if console(env).company().paused:
            console(env).resume("harness: the module that paused is over")


def sweep_repo_debris(env: dict, deadline_s: int = 300) -> None:
    """Close every open issue, close every open PR and delete every
    feature/* branch — the stragglers per-fixture cleanup cannot see (PRs
    born inside test bodies, fixtures that died before their teardown).
    Open issues are backlog: a leftover would be the lowest-numbered ticket
    the next session's heartbeat picks first. Issues go first: a delivered
    PR the platform finds closed is refused, and gives its ticket back to
    the loop if the ticket is still open. A sweep that cannot finish — a
    throttled token, a branch that will not delete — fails within
    `deadline_s`, by name."""
    with timed("repo sweep"):
        _sweep(env, target_slug(env), time.monotonic() + deadline_s)


def _sweep(env: dict, slug: str, deadline: float) -> None:
    _until_unlisted(
        env,
        f"/repos/{slug}/issues?state=open&per_page=100",
        deadline,
        lambda issue: _close_issue_now(env, slug, issue),
        keep=lambda issue: "pull_request" not in issue,
    )
    _until_unlisted(
        env,
        f"/repos/{slug}/pulls?state=open&per_page=100",
        deadline,
        lambda pull: close_pr(env, pull["number"]),
    )
    _until_unlisted(
        env,
        f"/repos/{slug}/branches?per_page=100",
        deadline,
        lambda branch: github_api(
            env, "DELETE", f"/repos/{slug}/git/refs/heads/{branch['name']}"
        ),
        keep=lambda branch: branch["name"].startswith("feature/"),
    )
    # Anything a refusal reopened to the loop meanwhile goes too.
    _until_unlisted(
        env,
        f"/repos/{slug}/issues?state=open&per_page=100",
        deadline,
        lambda issue: _close_issue_now(env, slug, issue),
        keep=lambda issue: "pull_request" not in issue,
    )


def _until_unlisted(
    env: dict,
    path: str,
    deadline: float,
    act: Callable[[dict], Any],
    keep: Callable[[dict], bool] = lambda item: True,
) -> None:
    """List `path`, `act` on every item `keep` selects, and list again
    until none is left. The host's listing lags its writes, so an item
    just closed may be listed once more: rounds are spaced, which spares
    the suite's token, and bounded by `deadline`."""
    while True:
        resp = github_api(env, "GET", path)
        assert resp.status_code == 200, f"listing {path}: {resp.status_code} {resp.text}"
        items = [item for item in resp.json() if keep(item)]
        if not items:
            return
        assert time.monotonic() < deadline, f"still listed by {path}: {items}"
        for item in items:
            act(item)
        time.sleep(3)


def _close_issue_now(env: dict, slug: str, issue: dict) -> None:
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
    heartbeat finds no backlog to steal — and no pause a module left is
    still holding it. Then the loop is given its idle tick on that empty
    backlog, so `test_quiet`'s successor session starts from rest."""
    yield
    try:
        sweep_repo_debris(env)
    finally:
        lift_pause(env)
    with contextlib.suppress(Exception):
        wake_heartbeat()
        await_heartbeat_asleep()


# --- the shared runs ---------------------------------------------------------


@pytest.fixture(scope="session")
def implement_run(stack: None, env: dict[str, str]) -> dict:
    """The happy-path run, with the worker killed mid-run — once the run's
    crew has its own pod, so the replay on the replacement worker inherits
    a live sandbox and live ephemeral agents to compensate: the company's
    own implement run on the ticket the suite filed.

    The PR's openness-at-birth is verified here, then the PR and the
    ticket are closed immediately: implement leaving the PR open is the
    product claim (it holds no merge or close authority), and a suite that
    parades open PRs across later modules hands the standing heartbeat
    backlog — reviews that race whichever module comes next. The ticket
    closes before the PR: a delivered PR found closed is refused, and an
    open ticket would come back to the loop."""
    record = run_implement("happy", env, kill_worker=True)
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    # What the delivery carried, read before the branch goes.
    number = pr_number(record.get("pr_url"))
    if number is not None:
        pulls = f"/repos/{target_slug(env)}/pulls/{number}"
        record["pr_files"] = [
            f["filename"] for f in github_api(env, "GET", f"{pulls}/files").json()
        ]
        record["pr_commits"] = github_api(env, "GET", f"{pulls}/commits").json()
    close_issue(env, record["issue_number"])
    close_pr_url(env, record.get("pr_url"))
    yield record


@pytest.fixture(scope="session")
def injected_run(stack: None, env: dict[str, str]) -> dict:
    """The injected ticket — the planted 'hotfix runbook' steers delivery
    onto `main`, the gateway denies it, the brain recovers onto feature/*;
    and, earlier in the same run, the verify-red scenario: the platform's
    own verify goes red where the implementer's advisory one was green,
    and a re-dispatch fixes it before delivery is released. Openness
    verified at birth, then closed — see `implement_run`."""
    record = run_implement("injected", env)
    record["pr_state_at_birth"] = pr_state(env, record.get("pr_url"))
    close_issue(env, record["issue_number"])
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
