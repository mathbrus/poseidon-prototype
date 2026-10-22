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
state, enforced stops, verified quiet — via four session-level guarantees:

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
- **One lane** (`run_ticket`, `run_review`, `apply_profile`,
  `sweep_repo_debris`, `repo_sweeper`): every run is the standing
  heartbeat's. A module files the ticket the loop should implement (or
  writes the strategy it should scope from), wakes the loop, and reads the
  run it started; what the loop may start on its own is declared in the
  profile the module applies (`blueprints.may_start`), never by a company
  without a heartbeat. Debris feeds contention — the loop treats any open
  PR or issue as backlog and wakes the moment any run finishes — so a
  module closes a PR as soon as its claims about it are read, or holds it
  open only under a profile the loop cannot start a review on; a module
  sweeps BEFORE it applies a profile and waits for the loop to be asleep
  on the swept backlog (`apply_profile`), so the ticks it counts from
  there are its own; and a run that is *meant* to fail runs under
  `blueprints.single_attempt`, or the loop retries it. A module that
  pauses the heartbeat resumes it before it ends — a paused loop never
  reports asleep, so the next profile apply would wait forever. The
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
tickets it filed; if it applies another blueprint profile, sweep before
the apply and `blueprints.restore_pristine()` after; bound the attempts
of a run it expects to fail; act through the console (`cluster.console`),
never by a raw Temporal signal — except a red-team proving the raw road
releases nothing; and anchor assertions on runs, gate events and tick
windows (`cluster.wake_and_await_ticks`), never on the ledger's whole
history.

Collection order is the hook's, not the filenames': the capstones run
after every module whose runs they read, and `test_quiet` runs last.
"""

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

from common.audit import gate_event
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

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    REPO_ROOT,
    await_bare_heartbeat_asleep,
    await_run,
    console,
    console_url,
    db_now,
    harness_repo_or_fail,
    kill_worker_pod,
    kubectl,
    run_by_task,
    run_kubectl,
    sandbox_pods,
    wake_bare_heartbeat,
)

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


# The mock brain's image, matched by repository name exactly: a lookalike
# (`poseidon-mock-brain-x`, a LiteLLM tag that mentions it) is a paid brain.
MOCK_BRAIN_IMAGE = "poseidon-mock-brain"


def image_repository(image: str) -> str:
    """`registry:5000/name:tag@sha256:…` → `name`."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


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
    if image_repository(image) == MOCK_BRAIN_IMAGE:
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
    standing heartbeat will find — and return its number once the git
    host lists it. GitHub lists a new issue up to a minute after creating
    it, and the loop reads the listing: a ticket the world filed is only
    filed once the world can see it."""
    created = github_api(
        env,
        "POST",
        f"/repos/{target_slug(env)}/issues",
        json={"title": title, "body": body},
    )
    assert created.status_code == 201, f"could not file issue: {created.text}"
    number = int(created.json()["number"])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        listed = github_api(
            env, "GET", f"/repos/{target_slug(env)}/issues?state=open&per_page=100"
        ).json()
        if any(int(i["number"]) == number for i in listed):
            return number
        time.sleep(5)
    raise AssertionError(f"the git host never listed issue #{number}")


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
    number: int, *, kill_after: str | None = None, deadline_s: int = 1800
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
    record = await_run(task, terminal=True, deadline_s=deadline_s, workflow="implement")
    return {**record, "issue_number": number}


def run_implement(name: str, env: dict, *, kill_after: str | None = None) -> dict:
    """File the scripted ticket `tickets/<name>.md` and let the heartbeat
    implement it. The ticket number rides the record for cleanup."""
    nonce = secrets.token_hex(3)
    number = file_ticket(env, f"{name} ({nonce})", ticket_body(name, nonce))
    return run_ticket(number, kill_after=kill_after)


def run_review(
    pr_number: int, approve: bool | None = None, deadline_s: int = 900
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
    deadline = time.monotonic() + deadline_s
    next_wake = 0.0
    while time.monotonic() < deadline:
        if run_by_task(task, workflow=workflow, since=since) is not None:
            return
        if time.monotonic() >= next_wake:
            wake_bare_heartbeat()
            next_wake = time.monotonic() + 60
        time.sleep(3)
    raise AssertionError(f"the heartbeat never started {task!r}")


def scope_via_heartbeat(
    env: dict, strategy: str, deadline_s: int = 600, *, keep_strategy: bool = False
) -> dict:
    """Write the strategy, wake the loop onto it, and return the terminal
    record of the scope run it starts. Every scope run works the same task
    text, so the run is the one born after this write — never an earlier
    module's.

    The strategy is a one-shot input: once the scope run is over, the
    tickets carry the slice and the resting strategy goes back. Left
    naming the slice, it would be scoped again the moment the slice's
    ticket closes with its merged pull request — the platform closes it —
    and the module's arc would grow a second one nobody asked for.
    `keep_strategy` keeps it naming the slice, for a module that tests
    what the loop does while the slice still looks open."""
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
        if not keep_strategy:
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
    return "/".join(env["TARGET_REPO"].split("/")[-2:])


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
    """The company the suite runs against: the pristine profile on the
    suite's clock (`blueprints.suite_document`). The bootstrap applied the
    example as written, whose gates wait a working day, and its heartbeat
    has been working whatever the repository held since — a vision-only
    repository gets its bootstrap tickets — so the suite starts from rest:
    the baseline is seeded first (the fixture dependency), the campsite
    swept, every run in flight allowed to end, and only then the pristine
    profile applied with the suite's own waits, so a module that parks on
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


def apply_profile(path: Path, env: dict) -> None:
    """Sweep, apply, converge, and wait for the standing heartbeat to be
    asleep — the way every module that applies its own profile must
    start. An empty campsite first: any open PR or issue an earlier module
    left becomes the loop's backlog the moment it reads the new section —
    a review parked on the gate that holds the one concurrency slot. Then
    the wait: with the loop asleep over the swept backlog, the ticks a
    module counts from here are its own — its runs wake the heartbeat
    forward, and `cluster.wake_and_await_ticks` stands in for the outside
    world."""
    sweep_repo_debris(env)
    lift_pause(env)
    apply_blueprint(path)
    wait_for_convergence()
    await_bare_heartbeat_asleep()


def lift_pause(env: dict) -> None:
    """Resume a heartbeat a module left paused — through the console, as
    the owner, so the resume is on the record like any other. A loop
    nobody paused needs nothing."""
    with contextlib.suppress(Exception):
        if console(env).company().paused:
            console(env).resume("harness: the module that paused is over")


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
    heartbeat finds no backlog to steal — and no pause a module left is
    still holding it. Then the loop is given its idle tick on that empty
    backlog, so `test_quiet`'s successor session starts from rest."""
    yield
    sweep_repo_debris(env)
    lift_pause(env)
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
    # What the delivery carried, read before the branch goes.
    number = pr_number(record.get("pr_url"))
    if number is not None:
        pulls = f"/repos/{target_slug(env)}/pulls/{number}"
        record["pr_files"] = [
            f["filename"] for f in github_api(env, "GET", f"{pulls}/files").json()
        ]
        record["pr_commits"] = github_api(env, "GET", f"{pulls}/commits").json()
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
