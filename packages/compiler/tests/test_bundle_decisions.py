"""The compiled bundle, evaluated by the real policy engine.

Golden files pin what we emit; these pin what it *decides* — which is the
part that matters. Every case here is a governance guarantee or a threat, so
a refactor of the emitter that quietly widens a scope fails here rather
than in production.

Needs Docker, which serves each bundle from the OPA image the chart deploys;
skips without it so a laptop without Docker still gets a green run (CI has
Docker).
"""

import copy
import http.client
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml
from conftest import HELLO_SAAS

from blueprint.catalog import ROLES
from blueprint.tools import PLATFORM_ONLY, SANDBOX_TOOLS
from common.policy import PLATFORM_DECISION_PATH, company_decision_path, rego_slug
from compiler import PLATFORM_SCOPE, compile_blueprint
from compiler.rego import REPO_TOOLS

# Pinned to the tag the chart deploys and CI's `opa` job checks against, so
# these decisions are the ones the OPA that will actually load the bundle
# would make.
OPA_IMAGE = "openpolicyagent/opa:1.19.0"

# The decision paths the gateways query: the company's own package, and the
# platform's bundle beside it. Both are loaded together here — their
# packages (and therefore bundle roots) are disjoint by construction.
COMPANY_QUERY = company_decision_path("hello-saas")
PLATFORM_QUERY = PLATFORM_DECISION_PATH

# The servers listen on the loopback only; no proxy stands between.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _docker(*args: str) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _published_url(container: str) -> str | None:
    """The loopback URL Docker published the server on, or None once the
    container has stopped: its port goes with it."""
    proc = subprocess.run(
        ["docker", "port", container, "8181/tcp"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return f"http://{proc.stdout.splitlines()[0]}" if proc.returncode == 0 else None


class OpaServers:
    """`opa run --server` containers, one per bundle directory, removed
    together."""

    def __init__(self) -> None:
        self._containers: list[str] = []

    def serve(self, bundle: Path) -> str:
        """Start a server on `bundle` and return its URL once it answers."""
        container = _docker(
            "run",
            "--detach",
            # As the invoking user, so reading the bundle directory does not
            # depend on the image's default uid matching whoever ran the tests.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{bundle}:/b:ro",
            # Docker picks a free loopback port; `docker port` reads it back.
            "--publish",
            "127.0.0.1::8181",
            OPA_IMAGE,
            "run",
            "--server",
            "--addr=0.0.0.0:8181",
            "/b",
        )
        self._containers.append(container)
        url = _published_url(container)
        deadline = time.monotonic() + 60
        while url is not None and time.monotonic() < deadline:
            try:
                with _LOCAL.open(f"{url}/health", timeout=5):
                    return url
            except (OSError, http.client.HTTPException):
                # A bundle OPA refuses to load stops the server: say why.
                if _docker("inspect", "--format={{.State.Running}}", container) != "true":
                    break
                time.sleep(0.05)
        logs = subprocess.run(
            ["docker", "logs", container], capture_output=True, text=True
        )
        pytest.fail(f"OPA did not serve {bundle}:\n{logs.stdout}{logs.stderr}")

    def remove_all(self) -> None:
        if self._containers:
            _docker("rm", "--force", *self._containers)
            self._containers.clear()


_SERVERS = OpaServers()


@pytest.fixture(scope="session", autouse=True)
def opa_servers():
    """The servers `_decider` starts, removed when the session ends, however
    its tests ended."""
    yield _SERVERS
    _SERVERS.remove_all()


def _decider(tmp_path_factory, blueprint: dict, platform: bool = True):
    """Compile the blueprint, write its bundle (with the platform bundle
    beside it unless `platform` is off), serve it, and return a query
    runner."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    plan = compile_blueprint(blueprint)
    bundle = tmp_path_factory.mktemp("bundle")
    files = {**plan.bundle.files, **(plan.platform_bundle.files if platform else {})}
    for name, body in files.items():
        (bundle / name).write_text(body)
    url = _SERVERS.serve(bundle)

    def run(_query: str = COMPANY_QUERY, **input_fields) -> dict:
        request = urllib.request.Request(
            f"{url}/v1/data/{_query}",
            data=json.dumps({"input": input_fields}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with _LOCAL.open(request, timeout=30) as response:
            answer = json.loads(response.read())
        assert "result" in answer, f"{_query} is undefined for {input_fields}"
        return answer["result"]

    return run


def _company(mutate=None) -> dict:
    """The shipped hello-saas blueprint, optionally edited in place."""
    blueprint = yaml.safe_load(HELLO_SAAS.read_text())
    if mutate is not None:
        mutate(blueprint)
    return blueprint


@pytest.fixture(scope="module")
def decide(tmp_path_factory):
    return _decider(tmp_path_factory, _company())


@pytest.fixture(scope="module")
def decide_heartbeat(tmp_path_factory):
    """The same company with implement as its only startable workflow."""

    def implement_only(blueprint: dict) -> None:
        blueprint["heartbeat"]["may_start"] = ["implement"]

    return _decider(tmp_path_factory, _company(implement_only))


# The example blueprint's repository: a repo-bearing call names it unless a
# test is about naming another one.
DECLARED_REPO = "github.com/example-org/hello-saas"


def _with_repo(extra: dict) -> dict:
    if extra.get("tool") in REPO_TOOLS:
        extra["args"] = {"repo": DECLARED_REPO, **(extra.get("args") or {})}
    return extra


def ephemeral(crew: str, role: str, **extra) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "ephemeral",
        "crew_template": crew,
        "role": role,
        "parent_agent_id": "pm-1",
        "run_id": "run-1",
        **_with_repo(extra),
    }


def standing(role: str, **extra) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "standing",
        "role": role,
        "run_id": "run-1",
        **_with_repo(extra),
    }


# -- the PM conducts and does not implement ---------------------------------


def test_pm_may_request_a_crew(decide):
    decision = decide(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "feature-review", "reason": "review PR"},
        )
    )
    assert decision["allow"] is True


def test_pm_cannot_call_implementer_tools(decide):
    """'The PM cannot call implementer-only tools' — a gateway deny,
    not prompt hope: the call a bound implementer wins, the PM loses to
    the default deny."""
    calls = [("git.open_pr", {"branch": "feature/x", "paths": ["slugify.py"]})]
    calls += [(tool, {}) for tool in SANDBOX_TOOLS]
    for tool, args in calls:
        call = {"tool": tool, "args": args, "run_binding_verified": True}
        as_implementer = decide(**ephemeral("feature-delivery", "implementer", **call))
        assert as_implementer == {
            "allow": True,
            "rule": f"crew_feature_delivery_implementer_{rego_slug(tool)}",
        }
        as_pm = decide(**standing("pm", **call))
        assert as_pm == {"allow": False, "rule": "default_deny"}, tool


# -- ephemeral agents are distinct principals ---------------------------------


def test_implementer_delivers_on_feature_branches(decide):
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "feature/slugify", "paths": ["slugify.py"]},
        )
    )
    assert decision["allow"] is True


def test_an_agent_works_on_the_declared_repository_only(decide):
    """A repo-bearing call names the company's repository or is denied by
    name, before the gateway reaches the git host — whoever the caller is
    and whatever else its scope allows."""
    ours = decide(**standing("pm", tool="issues.read", args={"number": 7}))
    assert ours["allow"] is True
    theirs = decide(
        **standing("pm", tool="issues.read", args={"repo": "github.com/other/repo"})
    )
    assert theirs == {"allow": False, "rule": "repo_not_declared"}
    unnamed = ephemeral(
        "feature-delivery",
        "implementer",
        tool="git.open_pr",
        args={"branch": "feature/x", "paths": ["slugify.py"]},
    )
    del unnamed["args"]["repo"]
    assert decide(**unnamed) == {"allow": False, "rule": "repo_not_declared"}


def test_reviewer_cannot_deliver(decide):
    """The delivery an implementer wins (above) is refused to the reviewer
    for want of the tool, not by any path or branch rule."""
    decision = decide(
        **ephemeral(
            "feature-review",
            "reviewer",
            tool="git.open_pr",
            args={"branch": "feature/slugify", "paths": ["slugify.py"]},
        )
    )
    assert decision == {"allow": False, "rule": "default_deny"}


# -- the blueprint's own policies ------------------------------------------


def test_protected_branch_is_denied(decide):
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "main", "paths": ["a.py"]},
        )
    )
    assert decision["allow"] is False
    assert decision["rule"].startswith("git_open_pr_")


def test_denied_paths_beat_an_allowed_branch(decide):
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": [".github/workflows/ci.yml"]},
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_denied_paths"}


# -- run binding, and the company's own rules -------------------------------


def test_sandbox_tools_require_a_verified_run_binding(decide):
    """A per-role allow-list without run binding reopens cross-run tool
    use; the gateway's registry enrichment is what sets this flag. The
    implementer bound to its run is allowed; unverified, or claiming no
    run, the same call is not."""
    call = ephemeral("feature-delivery", "implementer", tool="sandbox.exec", args={})
    bound = decide(**call, run_binding_verified=True)
    assert bound == {
        "allow": True,
        "rule": "crew_feature_delivery_implementer_sandbox_exec",
    }
    for unbound in (
        call,
        {**call, "run_binding_verified": False},
        {**call, "run_id": "", "run_binding_verified": True},
    ):
        assert decide(**unbound) == {"allow": False, "rule": "default_deny"}, unbound


def test_another_company_cannot_borrow_our_rules(decide):
    """Every rule names its company: the request our PM wins, made under
    another company's name, matches none of them."""
    request = standing(
        "pm",
        tool="crew.request",
        args={"crew_template": "feature-delivery", "reason": "ticket"},
    )
    assert decide(**request) == {"allow": True, "rule": "role_pm_crew_request"}
    borrowed = decide(**{**request, "company": "evil-corp"})
    assert borrowed == {"allow": False, "rule": "default_deny"}


# -- the platform lifecycle -----------------------------------------------


def _company_callers() -> list[tuple[dict, tuple[str, ...]]]:
    """Every caller of the shipped company beside the platform identity, as
    the gateway presents it, with the tools its catalog role holds: each
    standing agent, each ephemeral agent bound to its run, a human the
    company names, and the console."""
    plan = compile_blueprint(_company())
    callers = [
        (standing(i.role, agent_id=i.agent_id), ROLES[i.role].tools)
        for i in plan.instances
    ]
    callers += [
        ({**ephemeral(a.crew, a.role), "run_binding_verified": True}, ROLES[a.role].tools)
        for a in plan.ephemeral_agents
    ]
    callers += [(human("owner", ""), ()), (console_read(), ())]
    return callers


@pytest.mark.parametrize("tool", PLATFORM_SCOPE)
def test_the_platform_scope_is_the_platform_identitys_alone(decide, tool):
    """Every tool the orchestrator's own identity may call — the merge, the
    closing acts, the rework ticket, the notices, the archive, the reads,
    the verify gate, the sandbox registration — is allowed to it by a rule
    naming the tool, and to nobody else: the platform package refuses each
    company lifecycle the same call, and the company package refuses it to
    every caller — a platform-only tool to all of them, a tool some role
    shares (a read, a comment, a test run) to those whose role does not."""
    args = {"repo": DECLARED_REPO, "number": 4}
    call = {"company": "hello-saas", "run_id": "run-1", "tool": tool, "args": args}
    platform = decide(PLATFORM_QUERY, lifecycle="platform", **call)
    assert platform == {"allow": True, "rule": f"platform_{rego_slug(tool)}"}
    for caller, role_tools in _company_callers():
        presented = {**caller, "tool": tool, "args": args}
        who = (caller["lifecycle"], caller["role"])
        misrouted = decide(PLATFORM_QUERY, **presented)
        assert misrouted == {"allow": False, "rule": "default_deny"}, who
        if tool in PLATFORM_ONLY or tool not in role_tools:
            assert decide(**presented) == {"allow": False, "rule": "default_deny"}, who


def test_unknown_tool_is_denied_by_default(decide):
    decision = decide(**standing("pm", tool="issues.purge", args={}))
    assert decision == {"allow": False, "rule": "default_deny"}


def test_open_pr_without_paths_is_denied(decide):
    """Path rules fail closed: a git.open_pr whose input carries no
    args.paths cannot be checked against them, so it is denied (the
    gateway's bundle-apply always supplies the paths)."""
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "feature/x"},
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_paths_missing"}


# -- emitter edge cases (fresh bundles, not the module fixture) -------------


def _eval_bundle(tmp_path_factory, blueprint, input_fields) -> dict:
    """One company-only decision on a fresh bundle."""
    return _decider(tmp_path_factory, blueprint, platform=False)(**input_fields)


def test_bundle_without_policies_still_loads(tmp_path_factory, blueprint):
    """`policies` is optional: the entrypoint declares both rule sets, so a
    blueprint without it still emits a bundle OPA loads (no `deny_rules`
    unsafe var)."""
    del blueprint["policies"]
    decision = _eval_bundle(
        tmp_path_factory,
        blueprint,
        standing("pm", tool="issues.purge", args={}),
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_pattern_with_a_backtick_cannot_inject_policy(tmp_path_factory, blueprint):
    """The allowed_pattern is emitted as a JSON-escaped string, so a backtick
    cannot terminate the raw literal and splice text into the bundle."""
    blueprint["policies"]["branch_protection"]["allowed_pattern"] = "^feature/`x.*"
    decision = _eval_bundle(
        tmp_path_factory,
        blueprint,
        ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "feature/nope", "paths": ["a.py"]},
        ),
    )
    assert decision == {"allow": False, "rule": "git_open_pr_branch_pattern"}


def test_uppercase_protected_branch_still_protects(tmp_path_factory, blueprint):
    """`protected: [Main]` must deny `main`: branches are lowercased at emit
    time to match the lowercased input."""
    blueprint["policies"]["branch_protection"] = {"protected": ["Main"]}
    decision = _eval_bundle(
        tmp_path_factory,
        blueprint,
        ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "main", "paths": ["a.py"]},
        ),
    )
    assert decision == {"allow": False, "rule": "git_open_pr_protected_branch"}


def test_crew_rule_compares_the_raw_role_name():
    """The registry stores the role as written; only rule *names* are
    slugged. A slugged comparison would silently disable every rule of a
    hyphenated role — `contract-author` is the shipped one."""
    from compiler.plan import EphemeralAgentSpec
    from compiler.rego import build_bundle

    files = build_bundle(
        repo_url=DECLARED_REPO,
        base_ref="main",
        company="c",
        instances=[],
        ephemeral_agents=[
            EphemeralAgentSpec(
                crew="contract-authoring",
                role="contract-author",
                tools=("git.open_pr",),
            )
        ],
        policies={},
        heartbeat=compile_blueprint(_company()).heartbeat,
        documents={
            "strategy": "docs/STRATEGY.md",
            "architecture": "docs/ARCHITECTURE.md",
        },
        humans=("owner",),
    )
    body = files["crew_contract_authoring_contract_author.rego"]
    assert 'input.role == "contract-author"' in body
    assert "crew_contract_authoring_contract_author_git_open_pr" in body


def test_crew_request_is_narrowed_to_may_spawn(decide):
    """The compiled crew.request rule requires the requested crew to be in
    the conductor's may_spawn list — spawn intent outside it never even
    records."""
    allowed = decide(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "feature-delivery", "reason": "ticket"},
        )
    )
    assert allowed["allow"] is True

    outside = decide(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "shadow-ops", "reason": "hm"},
        )
    )
    assert outside == {"allow": False, "rule": "default_deny"}

    absent = decide(**standing("pm", tool="crew.request", args={}))
    assert absent == {"allow": False, "rule": "default_deny"}


def test_crew_request_concurrency_cap_denies_by_rule(decide):
    """The PM's max_concurrent (2 in hello-saas) is a compiled deny
    keyed on the instance — a first-class governance event, not a
    default-deny shrug. The count rides the enriched input."""
    under = decide(
        **standing("pm", tool="crew.request", args={"crew_template": "feature-delivery"}),
        agent_id="pm-1",
        active_spawns=1,
        budget_headroom=True,
    )
    assert under["allow"] is True

    over = decide(
        **standing("pm", tool="crew.request", args={"crew_template": "feature-delivery"}),
        agent_id="pm-1",
        active_spawns=2,
        budget_headroom=True,
    )
    assert over == {"allow": False, "rule": "crew_request_over_cap"}


def test_crew_request_without_budget_headroom_is_denied(decide):
    decision = decide(
        **standing("pm", tool="crew.request", args={"crew_template": "feature-delivery"}),
        agent_id="pm-1",
        active_spawns=0,
        budget_headroom=False,
    )
    assert decision == {"allow": False, "rule": "crew_request_no_budget"}


# -- the heartbeat's start authority ------------------------------


def heartbeat_start(workflow: str, **args) -> dict:
    """A platform heartbeat.start input. The target's prior attempts ride
    it like the other counts; a fresh item has none; the loop is not
    paused unless the test says so."""
    return {
        "company": "hello-saas",
        "lifecycle": "platform",
        "tool": "heartbeat.start",
        "args": {"workflow": workflow, "attempts": 0, "paused": False, **args},
    }


def test_the_heartbeat_may_start_scope(decide):
    """Scoping the next slice is the company's own initiative like any
    other start, bounded by the open-ticket cap."""
    decision = decide(
        **heartbeat_start("scope", active_runs=0, daily_spent_usd=0, open_tickets=2)
    )
    assert decision["allow"] is True
    at_cap = decide(
        **heartbeat_start("scope", active_runs=0, daily_spent_usd=0, open_tickets=5)
    )
    assert at_cap == {"allow": False, "rule": "heartbeat_start_over_open_tickets"}


def test_scope_fails_closed_without_the_open_ticket_count(decide):
    """Only scope reads the count, so only scope is refused without it:
    an implement start carries no open-ticket fact and needs none."""
    decision = decide(**heartbeat_start("scope", active_runs=0, daily_spent_usd=0))
    assert decision == {"allow": False, "rule": "heartbeat_start_open_tickets_missing"}
    implement = decide(**heartbeat_start("implement", active_runs=0, daily_spent_usd=0))
    assert implement["allow"] is True


def test_heartbeat_may_start_a_declared_workflow(decide_heartbeat):
    decision = decide_heartbeat(
        **heartbeat_start("implement", active_runs=0, daily_spent_usd=3.5)
    )
    assert decision["allow"] is True


def test_heartbeat_may_not_start_an_undeclared_workflow(decide_heartbeat):
    decision = decide_heartbeat(
        **heartbeat_start("review", active_runs=0, daily_spent_usd=0)
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_heartbeat_concurrency_cap_denies(decide_heartbeat):
    decision = decide_heartbeat(
        **heartbeat_start("implement", active_runs=1, daily_spent_usd=0)
    )
    assert decision["allow"] is False
    assert decision["rule"] == "heartbeat_start_over_concurrency"


def test_heartbeat_daily_cap_denies(decide_heartbeat):
    decision = decide_heartbeat(
        **heartbeat_start("implement", active_runs=0, daily_spent_usd=20)
    )
    assert decision["allow"] is False
    assert decision["rule"] == "heartbeat_start_over_daily_cap"


def test_heartbeat_attempt_cap_denies(decide_heartbeat):
    """`max_attempts: 2`: an item two runs already took up may not be
    started on again — the aborted-run retry is bounded by policy, not
    only by the observation that offers candidates."""
    decision = decide_heartbeat(
        **heartbeat_start("implement", active_runs=0, daily_spent_usd=0, attempts=2)
    )
    assert decision["allow"] is False
    assert decision["rule"] == "heartbeat_start_over_attempts"
    decision = decide_heartbeat(
        **heartbeat_start("implement", active_runs=0, daily_spent_usd=0, attempts=1)
    )
    assert decision["allow"] is True


@pytest.mark.parametrize(
    ("fact", "rule"),
    [
        ("active_runs", "heartbeat_start_counts_missing"),
        ("daily_spent_usd", "heartbeat_start_spend_missing"),
        ("attempts", "heartbeat_start_attempts_missing"),
    ],
)
def test_heartbeat_start_fails_closed_without_the_counts(decide_heartbeat, fact, rule):
    """Fail-safe defaults: the dangerous condition is the absent input,
    and absence denies — the changed-paths posture, applied to caps. The
    start allowed with every count is refused by name without any one."""
    start = heartbeat_start("implement", active_runs=0, daily_spent_usd=0)
    assert decide_heartbeat(**start) == {
        "allow": True,
        "rule": "heartbeat_start_implement",
    }
    del start["args"][fact]
    assert decide_heartbeat(**start) == {"allow": False, "rule": rule}


def test_a_paused_loop_starts_nothing_and_must_say_whether_it_is(decide_heartbeat):
    """The pause is a fact the caller reads fresh and rides on the input:
    true refuses the start by name, and an input that omits it refuses
    too — the absent fact is the dangerous one."""
    paused = decide_heartbeat(
        **heartbeat_start("implement", active_runs=0, daily_spent_usd=0, paused=True)
    )
    assert paused == {"allow": False, "rule": "heartbeat_start_while_paused"}
    unsaid = heartbeat_start("implement", active_runs=0, daily_spent_usd=0)
    del unsaid["args"]["paused"]
    decision = decide_heartbeat(**unsaid)
    assert decision == {"allow": False, "rule": "heartbeat_start_paused_missing"}


def test_agents_cannot_claim_heartbeat_authority(decide_heartbeat):
    """heartbeat.start allows only the platform lifecycle: a standing or
    ephemeral caller reciting the platform's whole start matches no rule."""
    start = heartbeat_start("implement", active_runs=0, daily_spent_usd=0)
    assert decide_heartbeat(**start) == {
        "allow": True,
        "rule": "heartbeat_start_implement",
    }
    claim = {"tool": start["tool"], "args": start["args"]}
    for caller in (
        standing("pm", **claim),
        ephemeral("feature-delivery", "implementer", **claim),
    ):
        decision = decide_heartbeat(**caller)
        assert decision == {"allow": False, "rule": "default_deny"}, caller["lifecycle"]


# -- the automated merge gate ----------------------------------------------


AUTOMATED_MERGE = {
    "deadline_minutes": 1440,
    "automate": {"paths": ["src/", "tests/"], "max_reworks": 2},
}


@pytest.fixture(scope="module")
def decide_automated(tmp_path_factory):
    """The same company with an automated merge gate declared."""

    def automated(blueprint: dict) -> None:
        blueprint["workflows"]["review"]["gates"]["merge"] = copy.deepcopy(
            AUTOMATED_MERGE
        )

    return _decider(tmp_path_factory, _company(automated))


def merge_resolve(**args) -> dict:
    # Independence and the contract fact default to true here so each test
    # isolates the layer it is about; the dedicated tests below flip them.
    # (Under a bundle with no contract tree the fact is simply unread.)
    # The resolver defaults to the automated path; the human-path tests below
    # name theirs.
    args.setdefault("resolver", "automated")
    args.setdefault("reviewer_independent", True)
    args.setdefault("contract_green", True)
    return {
        "company": "hello-saas",
        "lifecycle": "platform",
        "tool": "merge.resolve",
        "args": args,
    }


def test_no_automation_means_no_unattended_merge(decide):
    """Opt-in all the way down to the decision, the heartbeat.start posture:
    a gate without `automate` compiles no automated merge.resolve authority,
    so the automated path falls to default deny."""
    decision = decide(
        **merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_automated_merge_resolves_when_both_layers_pass(decide_automated):
    decision = decide_automated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/test_a.py"],
        )
    )
    assert decision == {"allow": True, "rule": "merge_resolve_automated"}


def test_a_steered_verdict_alone_merges_nothing(decide_automated):
    """The model-free layer is required: a reviewer talked into 'pass'
    cannot merge past red platform checks."""
    decision = decide_automated(
        **merge_resolve(verdict="pass", checks_green=False, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "merge_resolve_checks_not_green"}


def test_green_checks_without_a_passing_verdict_merge_nothing(decide_automated):
    decision = decide_automated(
        **merge_resolve(verdict="fail", checks_green=True, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "merge_resolve_verdict_not_pass"}


def test_a_path_not_automated_denies(decide_automated):
    decision = decide_automated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "deploy/values.yaml"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_path_not_automated"}


def test_merge_resolve_fails_closed_without_the_paths(decide_automated):
    """The git.open_pr posture: without the changed paths in the input the
    automated paths cannot be checked, so the resolve is denied by name."""
    decision = decide_automated(**merge_resolve(verdict="pass", checks_green=True))
    assert decision == {"allow": False, "rule": "merge_resolve_paths_missing"}


def test_an_empty_diff_is_not_an_automated_merge(decide_automated):
    """Every path of an empty diff is trivially automated, so the
    automation asks for at least one: with every fact green it matches no
    allow."""
    decision = decide_automated(
        **merge_resolve(verdict="pass", checks_green=True, paths=[])
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_a_carved_out_path_denies_by_name(decide_automated):
    """The belt under the compile-time refusal: a changed path under a
    carve-out denies the resolve whatever the automated paths say."""
    decision = decide_automated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", ".github/workflows/ci.yml"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_carved_out_path"}


def test_a_non_independent_review_merges_nothing(decide_automated):
    """The registry-checked fact is a required input: a reviewer that
    shares identity with the implementer (or cannot be attributed at all)
    denies by name."""
    decision = decide_automated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py"],
            reviewer_independent=False,
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_not_independent"}


def test_independence_fails_closed_when_absent(decide_automated):
    """Fail-safe defaults: the dangerous condition is the absent input."""
    args = merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    del args["args"]["reviewer_independent"]
    decision = decide_automated(**args)
    assert decision == {"allow": False, "rule": "merge_resolve_not_independent"}


def test_agents_cannot_claim_the_merge_resolve(decide_automated):
    """merge.resolve allows only the platform lifecycle: a standing or
    ephemeral caller reciting the platform's whole resolve, on either
    path, matches no rule."""
    for resolve, rule in (
        (
            merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"]),
            "merge_resolve_automated",
        ),
        (human_resolve({"principal": "owner", "approved": True}), "merge_resolve_human"),
    ):
        assert decide_automated(**resolve) == {"allow": True, "rule": rule}
        for caller in (standing("pm"), ephemeral("feature-review", "reviewer")):
            claimed = decide_automated(**{**resolve, **caller})
            assert claimed == {"allow": False, "rule": "default_deny"}, (rule, caller)


# -- architecture requests ----------------------------------------------------


def test_one_architecture_request_per_run(decide):
    """A bound ephemeral agent may ask once per run — the count rides the
    input from the gateway's own audit trail — and never without the
    count; the PM, who files tickets itself, has no such tool."""

    def request(role: str, crew: str, **extra: dict) -> dict:
        return {
            **ephemeral(crew, role, tool="architecture.request", args={"proposal": "p"}),
            "run_binding_verified": True,
            **extra,
        }

    first = decide(**request("implementer", "feature-delivery", architecture_requests=0))
    assert first["allow"] is True
    reviewer = decide(**request("reviewer", "feature-review", architecture_requests=0))
    assert reviewer["allow"] is True
    second = decide(**request("implementer", "feature-delivery", architecture_requests=1))
    assert second == {"allow": False, "rule": "architecture_request_over_cap"}
    uncounted = decide(**request("implementer", "feature-delivery"))
    assert uncounted == {"allow": False, "rule": "architecture_request_count_missing"}
    unbound = decide(
        **{
            **request("implementer", "feature-delivery", architecture_requests=0),
            "run_binding_verified": False,
        }
    )
    assert unbound == {"allow": False, "rule": "default_deny"}
    pm = decide(
        **standing("pm", tool="architecture.request", args={"proposal": "p"}),
        architecture_requests=0,
        run_binding_verified=True,
    )
    assert pm == {"allow": False, "rule": "default_deny"}


# -- dismissals ---------------------------------------------------------------


def _dismissal(**facts) -> dict:
    """The PM dismissing ticket #7 while it scopes, the gateway's facts as
    given (a fact named None is left out of the input)."""
    defaults = {
        "workflow": "scope",
        "run_live": True,
        "dismissals": 0,
        "dismiss_target": {"open": True, "bootstrap": False, "in_flight": False},
    }
    merged = {**defaults, **facts}
    return {
        **standing("pm", tool="issues.dismiss", args={"number": 7}),
        **{k: v for k, v in merged.items() if v is not None},
    }


def test_the_pm_dismisses_an_open_ticket_nobody_works_while_scoping(decide):
    assert decide(**_dismissal()) == {"allow": True, "rule": "role_pm_issues_dismiss"}


@pytest.mark.parametrize(
    ("facts", "rule"),
    [
        ({"workflow": "implement"}, "issues_dismiss_not_scoping"),
        ({"run_live": False}, "issues_dismiss_not_scoping"),
        (
            {"dismiss_target": {"open": False, "bootstrap": False, "in_flight": False}},
            "issues_dismiss_not_open",
        ),
        (
            {"dismiss_target": {"open": True, "bootstrap": True, "in_flight": False}},
            "issues_dismiss_bootstrap_ticket",
        ),
        (
            {"dismiss_target": {"open": True, "bootstrap": False, "in_flight": True}},
            "issues_dismiss_in_flight",
        ),
        ({"dismissals": 3}, "issues_dismiss_over_cap"),
        ({"run_live": None}, "issues_dismiss_facts_missing"),
        ({"dismissals": None}, "issues_dismiss_facts_missing"),
        ({"dismiss_target": None}, "issues_dismiss_facts_missing"),
        ({"dismiss_target": {"open": True}}, "issues_dismiss_facts_missing"),
    ],
)
def test_each_dismissal_limit_denies_by_name(decide, facts, rule):
    """Every limit is the compiled policy's, on facts the gateway reads;
    an absent fact denies rather than leaving the rule unevaluated."""
    assert decide(**_dismissal(**facts)) == {"allow": False, "rule": rule}


def test_only_the_pm_dismisses(decide):
    """No other role holds the tool, whatever the facts say: the dismissal
    the PM wins is refused to every ephemeral agent of the company."""
    facts = _dismissal()
    assert decide(**facts) == {"allow": True, "rule": "role_pm_issues_dismiss"}
    for crew, role in (
        ("feature-delivery", "implementer"),
        ("feature-review", "reviewer"),
        ("stewardship", "steward"),
    ):
        caller = {
            **facts,
            **ephemeral(crew, role, tool=facts["tool"], args=facts["args"]),
        }
        assert decide(**caller) == {"allow": False, "rule": "default_deny"}, role
    other_repo = _dismissal()
    other_repo["args"] = {"repo": "github.com/elsewhere/repo", "number": 7}
    assert decide(**other_repo) == {"allow": False, "rule": "repo_not_declared"}


# -- the company's documents ---------------------------------------------------


def _delivery(crew: str, role: str, paths: list[str], present: list[str] | None) -> dict:
    args = {"branch": "feature/doc", "paths": paths}
    if present is not None:
        args["paths_present"] = present
    return ephemeral(crew, role, tool="git.open_pr", args=args)


def test_only_the_steward_may_deliver_into_the_document_paths(decide):
    """The document fences, by name: an implementer touching either path
    is refused for the path's class; the steward may write the masterplan."""
    implementer = decide(
        **_delivery("feature-delivery", "implementer", ["docs/ARCHITECTURE.md"], [])
    )
    assert implementer == {"allow": False, "rule": "git_open_pr_architecture_path"}
    implementer = decide(
        **_delivery("feature-delivery", "implementer", ["docs/STRATEGY.md"], [])
    )
    assert implementer == {"allow": False, "rule": "git_open_pr_strategy_path"}
    steward = decide(**_delivery("stewardship", "steward", ["docs/ARCHITECTURE.md"], []))
    assert steward["allow"] is True
    revision = decide(
        **_delivery(
            "stewardship", "steward", ["docs/ARCHITECTURE.md"], ["docs/ARCHITECTURE.md"]
        )
    )
    assert revision["allow"] is True  # the masterplan is revised on request


def test_the_strategy_is_written_once(decide):
    """The steward may write the strategy while it is absent on the base
    ref and never afterwards; a strategy delivery without the presence
    fact fails closed — and a delivery elsewhere needs no such fact."""
    first = decide(**_delivery("stewardship", "steward", ["docs/STRATEGY.md"], []))
    assert first["allow"] is True
    again = decide(
        **_delivery("stewardship", "steward", ["docs/STRATEGY.md"], ["docs/STRATEGY.md"])
    )
    assert again == {"allow": False, "rule": "git_open_pr_strategy_present"}
    unknown = decide(**_delivery("stewardship", "steward", ["docs/STRATEGY.md"], None))
    assert unknown == {"allow": False, "rule": "git_open_pr_strategy_presence_missing"}
    elsewhere = decide(
        **_delivery("feature-delivery", "implementer", ["slugify.py"], None)
    )
    assert elsewhere["allow"] is True


# -- the contract tree --------------------------------------------------------


CONTRACT_TESTS = {"paths": ["tests/contract/"], "roles": ["contract-author"]}


@pytest.fixture(scope="module")
def decide_contract_tree(tmp_path_factory):
    """The company with a contract tree declared: acceptance tests only the
    contract-authoring crew's role may deliver into, beside automated paths
    that cannot contain them."""
    return _decider(tmp_path_factory, _company(_declare_contract_tree))


def _declare_contract_tree(blueprint: dict) -> None:
    """Declare the contract tree, its authoring crew, and automated paths
    beside it."""
    blueprint["policies"]["contract_tests"] = copy.deepcopy(CONTRACT_TESTS)
    _contract_crew(blueprint)


def _contract_crew(blueprint: dict) -> None:
    blueprint["standing"]["pm"]["may_spawn"]["crew"].append("contract-authoring")
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "deadline_minutes": 1440,
        "automate": {"paths": ["src/", "tests/unit/"], "max_reworks": 2},
    }


def test_the_implementer_cannot_pr_the_contract_tree(decide_contract_tree):
    """`git.open_pr` into the contract tree dies at the gateway for any
    role outside the declared owners — the PR never exists, and the audit
    row names the class, not an index."""
    decision = decide_contract_tree(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={
                "branch": "feature/x",
                "paths": ["slugify.py", "tests/contract/test_api.py"],
            },
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_contract_tree"}


def test_unit_tests_stay_the_implementers(decide_contract_tree):
    """The contract tree, not every test: everything outside it is
    ordinary code the implementer still owns end to end."""
    decision = decide_contract_tree(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={
                "branch": "feature/x",
                "paths": ["slugify.py", "tests/unit/test_slugify.py"],
            },
        )
    )
    assert decision["allow"] is True


def test_the_contract_author_delivers_into_the_contract_tree(decide_contract_tree):
    """The tree's positive half: the role the declaration names — the
    contract-authoring crew's distinct, deliver-capable contract author —
    opens the contract PR through the same governed `git.open_pr` as any
    change."""
    decision = decide_contract_tree(
        **ephemeral(
            "contract-authoring",
            "contract-author",
            tool="git.open_pr",
            args={
                "branch": "feature/slugify-contract",
                "paths": ["tests/contract/test_slugify.py"],
            },
        )
    )
    assert decision["allow"] is True


def test_a_contract_path_is_never_an_automated_merge(decide_contract_tree):
    """Every fact green and the resolve still denies by the class's own
    name: an edit to the contract tree never merges under the automation it
    grades — it parks for a person's verdict like any carve-out."""
    decision = decide_contract_tree(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/contract/test_api.py"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_contract_path"}


def test_the_automated_paths_still_resolve_beside_the_contract_tree(decide_contract_tree):
    decision = decide_contract_tree(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/unit/test_a.py"],
        )
    )
    assert decision == {"allow": True, "rule": "merge_resolve_automated"}


def test_a_red_contract_verify_merges_nothing(decide_contract_tree):
    """The baseline layer is required: a head suite weakened to green
    cannot launder a merge — the base-ref contract run is its own fact,
    and red denies by name."""
    decision = decide_contract_tree(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            contract_green=False,
            paths=["src/a.py"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_contract_not_green"}


def test_the_contract_fact_fails_closed_when_absent(decide_contract_tree):
    """A declared contract tree prices its fact into the automation: the
    absent key denies, the checks_not_green idiom."""
    args = merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    del args["args"]["contract_green"]
    decision = decide_contract_tree(**args)
    assert decision == {"allow": False, "rule": "merge_resolve_contract_not_green"}


def test_no_contract_tree_means_no_contract_fact(decide_automated):
    """Vacuous only by declaration: a company without `contract_tests`
    compiled no contract rule, so the automation reads exactly the facts
    it always did — the fact is not a new global requirement."""
    args = merge_resolve(
        verdict="pass", checks_green=True, paths=["src/a.py", "tests/test_a.py"]
    )
    del args["args"]["contract_green"]
    decision = decide_automated(**args)
    assert decision == {"allow": True, "rule": "merge_resolve_automated"}


# -- the contract tree's authority is not looser, and it revokes like authority


def test_the_contract_author_cannot_merge(decide_contract_tree):
    """Authority not looser: the deliver-capable contract-author role
    gains `git.open_pr` into its tree and nothing else — `git.merge_pr`
    stays platform-only."""
    as_author = decide_contract_tree(
        **ephemeral(
            "contract-authoring",
            "contract-author",
            tool="git.merge_pr",
            args={"number": 4},
        )
    )
    assert as_author == {"allow": False, "rule": "default_deny"}
    as_platform = decide_contract_tree(
        PLATFORM_QUERY, lifecycle="platform", tool="git.merge_pr", args={"number": 4}
    )
    assert as_platform == {"allow": True, "rule": "platform_git_merge_pr"}


def test_deny_paths_still_bind_the_contract_author(decide_contract_tree):
    """The blueprint's deny_paths outrank every grant for the contract
    author too: owning the contract tree buys no reach into a denied surface."""
    decision = decide_contract_tree(
        **ephemeral(
            "contract-authoring",
            "contract-author",
            tool="git.open_pr",
            args={
                "branch": "feature/x-contract",
                "paths": [".github/workflows/ci.yml", "tests/contract/test_api.py"],
            },
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_denied_paths"}


@pytest.fixture(scope="module")
def decide_revoked(tmp_path_factory):
    """`decide_contract_tree`'s company after the human revokes the contract tree:
    the same blueprint with `policies.contract_tests` deleted — one diff,
    recompiled. The crew and its spawn grant may outlive the declaration;
    the tree's special authority may not."""
    return _decider(tmp_path_factory, _company(_contract_crew))


def test_revocation_by_blueprint_diff_restores_the_single_tree(decide_revoked):
    """The contract tree is authority, so it revokes like authority: with
    the declaration gone the ownership deny is gone — the implementer
    reaches the tree again — and the automation reads the facts of an
    undeclared company, the contract fact not priced in. No looseness
    through the diff either: the revoked tree is ordinary code outside the
    automated paths, so its merges still park for a person — by the
    automated paths now, not by carve-out."""
    reopened = decide_revoked(
        **ephemeral(
            "feature-delivery",
            "implementer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": ["tests/contract/test_api.py"]},
        )
    )
    assert reopened == {
        "allow": True,
        "rule": "crew_feature_delivery_implementer_git_open_pr",
    }

    args = merge_resolve(
        verdict="pass", checks_green=True, paths=["src/a.py", "tests/unit/test_a.py"]
    )
    del args["args"]["contract_green"]
    resolved = decide_revoked(**args)
    assert resolved == {"allow": True, "rule": "merge_resolve_automated"}

    args["args"]["paths"] = ["src/a.py", "tests/contract/test_api.py"]
    parked = decide_revoked(**args)
    assert parked == {"allow": False, "rule": "merge_resolve_path_not_automated"}


# -- the human path of the merge gate, and the console's acts ---------------


def human(name: str, tool: str, **args) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "human",
        "role": "human",
        "agent_id": name,
        "tool": tool,
        "args": args,
    }


def human_resolve(record: dict | None, **args) -> dict:
    """A merge.resolve on the human path, with the console's recorded
    verdict (or none) riding the input."""
    if record is not None:
        args["human_verdict"] = record
    return merge_resolve(resolver="human", **args)


def test_a_human_may_resolve_the_merge_gate(decide):
    """The company's human answers the merge gate, either way, and the rule
    names the gate and the verdict — what the workflow reads back."""
    approve = decide(**human("owner", "gate.resolve", gate="merge", approved=True))
    reject = decide(**human("owner", "gate.resolve", gate="merge", approved=False))
    budget = decide(**human("owner", "gate.resolve", gate="budget", approved=True))
    assert approve == {"allow": True, "rule": "gate_resolve_merge_approve"}
    assert reject == {"allow": True, "rule": "gate_resolve_merge_reject"}
    assert budget == {"allow": True, "rule": "gate_resolve_budget_approve"}


def test_a_human_may_start_cancel_wake_pause_resume_apply_and_delete(decide):
    start = decide(**human("owner", "run.start", workflow="implement", number=4))
    scope = decide(**human("owner", "run.start", workflow="scope"))
    cancel = decide(**human("owner", "run.cancel", run_id="run-1"))
    wake = decide(**human("owner", "heartbeat.wake"))
    pause = decide(**human("owner", "heartbeat.pause", reason="reading the ledger"))
    resume = decide(**human("owner", "heartbeat.resume", reason="read"))
    apply = decide(**human("owner", "blueprint.apply"))
    delete = decide(**human("owner", "company.delete", purge_remote=False))
    assert start == {"allow": True, "rule": "run_start_implement"}
    assert scope == {"allow": False, "rule": "default_deny"}
    assert cancel == {"allow": True, "rule": "run_cancel"}
    assert wake == {"allow": True, "rule": "heartbeat_wake"}
    assert pause == {"allow": True, "rule": "heartbeat_pause"}
    assert resume == {"allow": True, "rule": "heartbeat_resume"}
    assert apply == {"allow": True, "rule": "blueprint_apply"}
    assert delete == {"allow": True, "rule": "company_delete"}


def test_a_delete_names_whether_it_purges_and_each_answer_has_its_rule(decide):
    """Like a verdict: the rule says which delete was allowed, and a delete
    that does not say whether it purges the repository is not."""
    purge = decide(**human("owner", "company.delete", purge_remote=True))
    unsaid = decide(**human("owner", "company.delete"))
    assert purge == {"allow": True, "rule": "company_delete_purge_remote"}
    assert unsaid == {"allow": False, "rule": "default_deny"}


# Every act a person takes on the company through the console, as the
# console puts it to policy, with the rule that allows it to a human the
# company names.
HUMAN_ACTS = [
    ("gate.resolve", {"gate": "merge", "approved": True}, "gate_resolve_merge_approve"),
    ("run.start", {"workflow": "implement", "number": 4}, "run_start_implement"),
    ("run.cancel", {"run_id": "run-1"}, "run_cancel"),
    ("heartbeat.wake", {}, "heartbeat_wake"),
    ("heartbeat.pause", {"reason": "mine now"}, "heartbeat_pause"),
    ("heartbeat.resume", {"reason": "mine now"}, "heartbeat_resume"),
    ("blueprint.apply", {}, "blueprint_apply"),
    ("company.delete", {"purge_remote": True}, "company_delete_purge_remote"),
]


@pytest.mark.parametrize(
    ("tool", "args", "rule"), HUMAN_ACTS, ids=[act for act, _, _ in HUMAN_ACTS]
)
def test_an_unlisted_human_is_denied_by_name(decide, tool, args, rule):
    """A verified person the company did not name is refused by a rule that
    says so — an audit row, not an unfired allow — whatever the act; the
    same act by a human the company names is allowed."""
    assert decide(**human("owner", tool, **args)) == {"allow": True, "rule": rule}
    stranger = decide(**human("stranger", tool, **args))
    assert stranger == {"allow": False, "rule": "human_not_recognized"}


def test_an_agent_cannot_claim_human_authority(decide):
    """The rules key on the lifecycle: an agent presenting a human's name
    and verdict gets nothing, and a human presenting an agent's call gets
    nothing — each call allowed to its rightful caller."""
    verdict = human("owner", "gate.resolve", gate="merge", approved=True)
    assert decide(**verdict) == {"allow": True, "rule": "gate_resolve_merge_approve"}
    as_pm = decide(**{**verdict, **standing("pm")})
    assert as_pm == {"allow": False, "rule": "default_deny"}

    ticket = standing("pm", tool="issues.create", args={"title": "t", "body": "b"})
    assert decide(**ticket) == {"allow": True, "rule": "role_pm_issues_create"}
    as_human = decide(**{**ticket, **human("owner", ticket["tool"], **ticket["args"])})
    assert as_human == {"allow": False, "rule": "default_deny"}


def console_read(
    lifecycle: str = "console", tool: str = "repo.read_file", **args
) -> dict:
    """The console's call at the MCP gateway, as the gateway puts it to
    policy: its own lifecycle, no run."""
    read = {
        "run_id": "",
        "repo": DECLARED_REPO,
        "ref": "main",
        "path": "docs/STRATEGY.md",
        **args,
    }
    return {
        "company": "hello-saas",
        "agent_id": "console",
        "role": "console",
        "lifecycle": lifecycle,
        "parent_agent_id": "",
        "run_id": "",
        "workflow": "",
        "tool": tool,
        "args": read,
        "run_binding_verified": False,
    }


def test_the_console_reads_the_two_documents_and_nothing_else(decide):
    """The console's whole authority at the gateway: `repo.read_file` of
    the company's two document paths, on its repository, at its base ref.
    Any other tool, path, ref or repository is denied — and no other
    lifecycle holds the rule."""
    for path in ("docs/STRATEGY.md", "docs/ARCHITECTURE.md"):
        assert decide(**console_read(path=path)) == {
            "allow": True,
            "rule": "console_read_document",
        }
    denied = [
        console_read(path="README.md"),
        console_read(path="../docs/STRATEGY.md"),
        console_read(ref="feature/x"),
        console_read(repo="github.com/attacker/repo"),
        console_read(tool="repo.archive"),
        console_read(tool="git.merge_pr", number=1),
        console_read(tool="issues.create", title="t", body="b"),
        console_read(lifecycle="standing"),
    ]
    for call in denied:
        assert decide(**call) == {"allow": False, "rule": "default_deny"}, call
    # Not even a human the company names reads through the console's rule.
    as_owner = decide(**{**console_read(lifecycle="human"), "agent_id": "owner"})
    assert as_owner == {"allow": False, "rule": "default_deny"}


def console_purge(lifecycle: str = "console", **args) -> dict:
    """The console's purge at the MCP gateway, as the gateway puts it."""
    call = console_read(lifecycle, "repo.purge")
    call["args"] = {"run_id": "", "repo": DECLARED_REPO, "base_ref": "main", **args}
    return call


def test_only_the_console_may_purge_and_only_the_declared_repository(decide):
    """`repo.purge` resets a repository for a person deleting the company:
    the console alone holds it, on the company's repository at its base
    ref. Not the platform identity, no role, no person directly."""
    assert decide(**console_purge()) == {"allow": True, "rule": "console_repo_purge"}
    denied = [
        console_purge(repo="github.com/attacker/repo"),
        console_purge(base_ref="feature/x"),
        console_purge(lifecycle="standing"),
        console_purge(lifecycle="ephemeral"),
    ]
    for call in denied:
        assert decide(**call) == {"allow": False, "rule": "default_deny"}, call
    as_owner = decide(**{**console_purge(lifecycle="human"), "agent_id": "owner"})
    assert as_owner == {"allow": False, "rule": "default_deny"}
    as_platform = decide(PLATFORM_QUERY, **console_purge(lifecycle="platform"))
    assert as_platform == {"allow": False, "rule": "default_deny"}
    as_pm = decide(
        **standing(
            "pm", tool="repo.purge", args={"repo": DECLARED_REPO, "base_ref": "main"}
        )
    )
    assert as_pm == {"allow": False, "rule": "default_deny"}


def test_a_recorded_human_verdict_releases_the_merge(decide):
    decision = decide(**human_resolve({"principal": "owner", "approved": True}))
    assert decision == {"allow": True, "rule": "merge_resolve_human"}


def test_merge_resolve_human_needs_a_recorded_verdict(decide):
    """A signal that reached the workflow without a console-recorded
    verdict releases nothing: the deny names the missing artifact."""
    decision = decide(**human_resolve(None))
    assert decision == {"allow": False, "rule": "merge_resolve_no_human_verdict"}


def test_a_recorded_rejection_releases_nothing(decide):
    decision = decide(**human_resolve({"principal": "owner", "approved": False}))
    assert decision == {"allow": False, "rule": "merge_resolve_human_rejected"}


def test_a_non_human_verdict_cannot_release(decide):
    """A verdict recorded under a name the company did not list — a human
    the deployment knows but the blueprint does not — releases nothing."""
    decision = decide(**human_resolve({"principal": "stranger", "approved": True}))
    assert decision == {"allow": False, "rule": "merge_resolve_not_a_human"}


def test_merge_resolve_fails_closed_without_a_resolver(decide):
    decision = decide(**{**merge_resolve(), "args": {}})
    assert decision == {"allow": False, "rule": "merge_resolve_resolver_missing"}


def test_a_human_release_ignores_automation_facts(decide_automated):
    """The human override: a recorded approving verdict releases a change
    automation would refuse — a carve-out, a path not automated, red
    checks — because the person, not the automated paths, is the authority."""
    decision = decide_automated(
        **human_resolve(
            {"principal": "owner", "approved": True},
            verdict="fail",
            checks_green=False,
            paths=[".github/workflows/ci.yml", "docs/x.md"],
        )
    )
    assert decision == {"allow": True, "rule": "merge_resolve_human"}


def test_automation_denies_stay_on_the_automated_path(decide_automated):
    """And the converse: the automated path is held to every automation fact
    whatever verdict a human may have recorded."""
    decision = decide_automated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=[".github/workflows/ci.yml"],
            human_verdict={"principal": "owner", "approved": True},
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_carved_out_path"}
