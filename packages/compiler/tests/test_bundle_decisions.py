"""The compiled bundle, evaluated by the real policy engine.

Golden files pin what we emit; these pin what it *decides* — which is the
part that matters. Every case here is a governance guarantee or a threat, so
a refactor of the emitter that quietly widens a scope fails here rather
than in production.

Needs the `opa` binary or Docker; skips when neither is present so a laptop
without them still gets a green run (CI has Docker).
"""

import copy
import json
import os
import shutil
import subprocess

import pytest
import yaml
from conftest import HEARTBEAT_SECTION, HELLO_SAAS

from compiler import compile_blueprint

# Pinned to the tag the chart deploys and CI's `opa` job checks against, so
# these decisions are the ones the OPA that will actually load the bundle
# would make.
OPA_IMAGE = "openpolicyagent/opa:1.19.0"

# Per-company packages; the platform scope lives in its
# own bundle. Both are loaded together here — their packages (and therefore
# bundle roots) are disjoint by construction.
COMPANY_QUERY = "data.poseidon.authz.hello_saas.decision"
PLATFORM_QUERY = "data.poseidon.platform.decision"


def _runner():
    if shutil.which("opa"):
        return lambda bundle, stdin, query: subprocess.run(
            ["opa", "eval", "-d", bundle, "-f", "raw", "-I", query],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )
    if shutil.which("docker"):
        return lambda bundle, stdin, query: subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                # As the invoking user, so reading the bundle directory does
                # not depend on the image's default uid matching whoever ran
                # the tests.
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{bundle}:/b:ro",
                OPA_IMAGE,
                "eval",
                "-d",
                "/b",
                "-f",
                "raw",
                "-I",
                query,
            ],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return None


def _decider(tmp_path_factory, blueprint: dict, platform: bool = True):
    """Compile the blueprint, write its bundle (with the platform bundle
    beside it unless `platform` is off), and return a query runner."""
    runner = _runner()
    if runner is None:
        pytest.skip("neither the opa binary nor docker is available")
    plan = compile_blueprint(blueprint)
    bundle = tmp_path_factory.mktemp("bundle")
    files = {**plan.bundle.files, **(plan.platform_bundle.files if platform else {})}
    for name, body in files.items():
        (bundle / name).write_text(body)

    def run(_query: str = COMPANY_QUERY, **input_fields) -> dict:
        proc = runner(str(bundle), json.dumps(input_fields), _query)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

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
    """The same company with a standing heartbeat declared."""

    def add_heartbeat(blueprint: dict) -> None:
        blueprint["heartbeat"] = copy.deepcopy(HEARTBEAT_SECTION)

    return _decider(tmp_path_factory, _company(add_heartbeat))


def ephemeral(crew: str, agent: str, template: str, **extra) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "ephemeral",
        "crew_template": crew,
        "crew_role": agent,
        "role": template,
        "parent_agent_id": "pm-1",
        "run_id": "run-1",
        **extra,
    }


def standing(role: str, **extra) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "standing",
        "role": role,
        "run_id": "run-1",
        **extra,
    }


# -- the lean-standing exit criteria ----------------------------------------


def test_pm_may_request_a_crew(decide):
    decision = decide(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "feature-review", "reason": "review PR"},
        )
    )
    assert decision["allow"] is True


def test_pm_cannot_call_executor_tools(decide):
    """'The PM cannot call executor-only tools' — a gateway deny,
    not prompt hope."""
    for tool in ("git.open_pr", "sandbox.write_file", "sandbox.exec"):
        decision = decide(**standing("pm", tool=tool, args={"branch": "feature/x"}))
        assert decision["allow"] is False, tool


# -- ephemeral agents are distinct principals ---------------------------------


def test_planner_is_read_only(decide):
    """Both agents run the `backend-engineer` template, so without the
    crew-slot discriminator the planner would inherit delivery rights."""
    read = decide(
        **ephemeral(
            "feature-delivery",
            "planner",
            "backend-engineer",
            tool="sandbox.read_file",
            run_binding_verified=True,
            args={},
        )
    )
    write = decide(
        **ephemeral(
            "feature-delivery",
            "planner",
            "backend-engineer",
            tool="sandbox.write_file",
            run_binding_verified=True,
            args={},
        )
    )
    deliver = decide(
        **ephemeral(
            "feature-delivery",
            "planner",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": []},
        )
    )
    assert read["allow"] is True
    assert write["allow"] is False
    assert deliver["allow"] is False


def test_executor_delivers_on_feature_branches(decide):
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/slugify", "paths": ["slugify.py"]},
        )
    )
    assert decision["allow"] is True


def test_reviewer_cannot_deliver(decide):
    decision = decide(
        **ephemeral(
            "feature-review",
            "reviewer",
            "sdet",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": []},
        )
    )
    assert decision["allow"] is False


# -- the blueprint's own policies ------------------------------------------


def test_protected_branch_is_denied(decide):
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
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
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": [".github/workflows/ci.yml"]},
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_denied_paths"}


# -- run binding and tenancy ----------------------------------------------


def test_sandbox_tools_require_a_verified_run_binding(decide):
    """A per-role allow-list without run binding reopens cross-run tool
    use; the gateway's registry enrichment is what sets this flag."""
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="sandbox.exec",
            args={},
        )
    )
    assert decision["allow"] is False


def test_another_company_cannot_borrow_our_rules(decide):
    decision = decide(
        company="evil-corp",
        lifecycle="standing",
        role="pm",
        tool="crew.request",
        run_id="run-1",
        args={},
    )
    assert decision["allow"] is False


# -- the platform lifecycle -----------------------------------------------


def test_only_the_platform_may_merge(decide):
    """The workflow merges under its own identity after the human
    gate — via its own decision path; no company role can reach the tool in
    the company package at all."""
    assert (
        decide(
            PLATFORM_QUERY,
            lifecycle="platform",
            tool="git.merge_pr",
            run_id="run-1",
            args={},
        )["allow"]
        is True
    )
    assert (
        decide(
            **ephemeral(
                "feature-delivery",
                "executor",
                "backend-engineer",
                tool="git.merge_pr",
                args={},
            )
        )["allow"]
        is False
    )


def test_unknown_tool_is_denied_by_default(decide):
    decision = decide(**standing("pm", tool="issues.purge", args={}))
    assert decision == {"allow": False, "rule": "default_deny"}


def test_platform_may_retire_a_sandbox_registration(decide):
    """Both gateway admin routes are in PLATFORM_SCOPE — teardown needs the
    DELETE as much as fulfilment needs the POST."""
    for tool in ("/admin/sandboxes", "/admin/sandboxes.delete"):
        assert (
            decide(
                PLATFORM_QUERY, lifecycle="platform", tool=tool, run_id="run-1", args={}
            )["allow"]
            is True
        ), tool


def test_company_lifecycles_find_nothing_in_the_platform_package(decide):
    """The platform package carries only platform rules: a standing or
    ephemeral caller routed there by mistake still gets default-deny."""
    assert decide(
        PLATFORM_QUERY,
        **standing(
            "backend-engineer", tool="sandbox.exec", run_binding_verified=True, args={}
        ),
    ) == {"allow": False, "rule": "default_deny"}


def test_open_pr_without_paths_is_denied(decide):
    """Path rules fail closed: a git.open_pr whose input carries no
    args.paths cannot be checked against them, so it is denied (the
    gateway's bundle-apply always supplies the paths)."""
    decision = decide(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/x"},
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_paths_missing"}


# -- emitter edge cases (fresh bundles, not the module fixture) -------------


def _eval_bundle(tmp_path_factory, blueprint, name, input_fields) -> dict:
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
        "no-policies",
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
        "backtick-pattern",
        ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
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
        "uppercase-protected",
        ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "main", "paths": ["a.py"]},
        ),
    )
    assert decision == {"allow": False, "rule": "git_open_pr_protected_branch"}


def test_crew_rule_compares_the_raw_member_name():
    """`run_participants.crew_role` stores the agent name as written; only
    rule *names* are slugged. A slugged comparison would silently disable
    every rule of a hyphenated agent."""
    from compiler.plan import EphemeralAgentSpec
    from compiler.rego import build_bundle

    files = build_bundle(
        company="c",
        instances=[],
        ephemeral_agents=[
            EphemeralAgentSpec(
                crew="feature-review",
                crew_role="code-reviewer",
                role_template="sdet",
                tools=("git.read",),
                needs_sandbox=True,
                budget_inheritance="run",
            )
        ],
        policies={},
    )
    body = files["crew_feature_review_code_reviewer.rego"]
    assert 'input.crew_role == "code-reviewer"' in body
    assert "crew_feature_review_code_reviewer_git_read" in body


def test_crew_request_is_narrowed_to_may_spawn(decide):
    """The compiled crew.request rule requires the requested
    template to be in the conductor's may_spawn list — spawn intent outside
    it never even records."""
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
    assert absent["allow"] is False


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
    it like the other counts; a fresh item has none."""
    return {
        "company": "hello-saas",
        "lifecycle": "platform",
        "tool": "heartbeat.start",
        "args": {"workflow": workflow, "attempts": 0, **args},
    }


def test_no_heartbeat_section_means_no_unattended_start(decide):
    """The section is opt-in all the way down to the decision: a company
    that never declared a heartbeat compiles no heartbeat rules, so the
    start decision falls to default deny."""
    decision = decide(**heartbeat_start("implement", active_runs=0, daily_spent_usd=0))
    assert decision == {"allow": False, "rule": "default_deny"}


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


def test_heartbeat_start_fails_closed_without_the_counts(decide_heartbeat):
    """Fail-safe defaults: the dangerous condition is the absent input,
    and absence denies — the changed-paths posture, applied to caps."""
    decision = decide_heartbeat(**heartbeat_start("implement", daily_spent_usd=0))
    assert decision["allow"] is False
    assert decision["rule"] == "heartbeat_start_counts_missing"
    absent_attempts = heartbeat_start("implement", active_runs=0, daily_spent_usd=0)
    del absent_attempts["args"]["attempts"]
    decision = decide_heartbeat(**absent_attempts)
    assert decision["allow"] is False
    assert decision["rule"] == "heartbeat_start_attempts_missing"


def test_agents_cannot_claim_heartbeat_authority(decide_heartbeat):
    """heartbeat.start allows only the platform lifecycle: a standing or
    ephemeral caller reciting the tool name matches no rule."""
    decision = decide_heartbeat(
        **standing("pm", tool="heartbeat.start"),
        args={
            "workflow": "implement",
            "active_runs": 0,
            "daily_spent_usd": 0,
            "attempts": 0,
        },
    )
    assert decision["allow"] is False


def test_heartbeat_grants_no_new_path_to_merge(decide_heartbeat):
    """Authority not looser: declaring a heartbeat adds start authority for
    the platform and nothing else — the merge stays reachable only through
    the platform identity after the human gate, exactly as without it."""
    as_pm = decide_heartbeat(**standing("pm", tool="git.merge_pr", args={"number": 4}))
    assert as_pm["allow"] is False
    as_platform = decide_heartbeat(
        PLATFORM_QUERY, lifecycle="platform", tool="git.merge_pr", args={"number": 4}
    )
    assert as_platform["allow"] is True  # same as the human-started bundle


# -- the policy-resolved merge gate ----------------------------------------


DELEGATED_MERGE = {
    "resolved_by": "policy",
    "delegate": {"paths": ["src/", "tests/"], "max_reworks": 2},
}


@pytest.fixture(scope="module")
def decide_delegated(tmp_path_factory):
    """The same company with a policy-resolved merge gate declared."""

    def delegated(blueprint: dict) -> None:
        blueprint["workflows"]["review"]["gates"]["merge"] = copy.deepcopy(
            DELEGATED_MERGE
        )

    return _decider(tmp_path_factory, _company(delegated))


def merge_resolve(**args) -> dict:
    # Independence and the contract fact default to true here so each test
    # isolates the layer it is about; the dedicated tests below flip them.
    # (Under a bundle with no contract tree the fact is simply unread.)
    args.setdefault("reviewer_independent", True)
    args.setdefault("contract_green", True)
    return {
        "company": "hello-saas",
        "lifecycle": "platform",
        "tool": "merge.resolve",
        "args": args,
    }


def test_no_delegation_means_no_unattended_merge(decide):
    """Opt-in all the way down to the decision, the heartbeat.start posture:
    a human-resolved gate compiles no merge.resolve authority, so the
    decision falls to default deny."""
    decision = decide(
        **merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_delegated_merge_resolves_when_both_layers_pass(decide_delegated):
    decision = decide_delegated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/test_a.py"],
        )
    )
    assert decision == {"allow": True, "rule": "merge_resolve_delegated"}


def test_a_steered_verdict_alone_merges_nothing(decide_delegated):
    """The model-free layer is required: a reviewer talked into 'pass'
    cannot merge past red platform checks."""
    decision = decide_delegated(
        **merge_resolve(verdict="pass", checks_green=False, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "merge_resolve_checks_not_green"}


def test_green_checks_without_a_passing_verdict_merge_nothing(decide_delegated):
    decision = decide_delegated(
        **merge_resolve(verdict="fail", checks_green=True, paths=["src/a.py"])
    )
    assert decision == {"allow": False, "rule": "merge_resolve_verdict_not_pass"}


def test_a_path_outside_the_delegated_class_denies(decide_delegated):
    decision = decide_delegated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "deploy/values.yaml"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_undelegated_path"}


def test_merge_resolve_fails_closed_without_the_paths(decide_delegated):
    """The git.open_pr posture: without the changed paths in the input the
    class cannot be checked, so the resolve is denied by name."""
    decision = decide_delegated(**merge_resolve(verdict="pass", checks_green=True))
    assert decision == {"allow": False, "rule": "merge_resolve_paths_missing"}


def test_an_empty_diff_is_not_a_merge_policy_resolves(decide_delegated):
    decision = decide_delegated(
        **merge_resolve(verdict="pass", checks_green=True, paths=[])
    )
    assert decision["allow"] is False


def test_a_carved_out_path_denies_by_name(decide_delegated):
    """The belt under the compile-time refusal: a changed path under a
    carve-out denies the resolve whatever the delegated class says."""
    decision = decide_delegated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", ".github/workflows/ci.yml"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_carved_out_path"}


def test_a_non_independent_review_merges_nothing(decide_delegated):
    """The registry-checked fact is a required input: a reviewer that
    shares identity with the implementer (or cannot be attributed at all)
    denies by name."""
    decision = decide_delegated(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py"],
            reviewer_independent=False,
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_not_independent"}


def test_independence_fails_closed_when_absent(decide_delegated):
    """Fail-safe defaults: the dangerous condition is the absent input."""
    args = merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    del args["args"]["reviewer_independent"]
    decision = decide_delegated(**args)
    assert decision == {"allow": False, "rule": "merge_resolve_not_independent"}


def test_agents_cannot_claim_the_merge_resolve(decide_delegated):
    """merge.resolve allows only the platform lifecycle: a standing caller
    reciting the tool name matches no rule."""
    decision = decide_delegated(
        **standing("pm", tool="merge.resolve"),
        args={"verdict": "pass", "checks_green": True, "paths": ["src/a.py"]},
    )
    assert decision["allow"] is False


def test_delegation_grants_no_new_path_to_merge_pr(decide_delegated):
    """Authority not looser: the delegation adds the resolve decision and
    nothing else — git.merge_pr stays unreachable for every company role."""
    as_pm = decide_delegated(**standing("pm", tool="git.merge_pr", args={"number": 4}))
    assert as_pm["allow"] is False


def test_the_rework_write_belongs_to_the_platform(decide_delegated):
    """file_rework files the ticket under the platform identity; the
    reviewer that produced the findings holds no issues.create at all —
    a steered reviewer can fail a PR, never flood the backlog itself."""
    as_platform = decide_delegated(
        PLATFORM_QUERY,
        lifecycle="platform",
        tool="issues.create",
        run_id="run-1",
        args={"title": "Rework 1 for PR #41: x", "body": "…"},
    )
    assert as_platform["allow"] is True
    as_reviewer = decide_delegated(
        **ephemeral(
            "feature-review",
            "reviewer",
            "sdet",
            tool="issues.create",
            args={"title": "Rework 1 for PR #41: x", "body": "…"},
        )
    )
    assert as_reviewer["allow"] is False


# -- the contract tree --------------------------------------------------------


CONTRACT_TESTS = {"paths": ["tests/contract/"], "roles": ["contract-author"]}

CONTRACT_CREW = {
    "agents": {
        "author": {
            "role_template": "contract-author",
            "tools": [
                "sandbox.read_file",
                "sandbox.write_file",
                "sandbox.edit_file",
                "sandbox.list_dir",
                "sandbox.exec",
                "sandbox.run_tests",
                "sandbox.git_bundle",
                "git.open_pr",
            ],
        }
    },
    "budget_inheritance": "run",
}


@pytest.fixture(scope="module")
def decide_split(tmp_path_factory):
    """The company with the oracle split declared: a protected contract
    tree owned by the contract-authoring crew's template, beside a
    delegated class that cannot contain it."""
    return _decider(tmp_path_factory, _company(_split))


def _split(blueprint: dict) -> None:
    """Declare the contract tree, its authoring crew, and a delegated class
    beside it."""
    blueprint["policies"]["contract_tests"] = copy.deepcopy(CONTRACT_TESTS)
    _contract_crew(blueprint)


def _contract_crew(blueprint: dict) -> None:
    blueprint["crews"]["contract-authoring"] = copy.deepcopy(CONTRACT_CREW)
    blueprint["standing"]["pm"]["may_spawn"]["crew"].append("contract-authoring")
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "resolved_by": "policy",
        "delegate": {"paths": ["src/", "tests/unit/"], "max_reworks": 2},
    }


def test_the_implementer_cannot_pr_the_contract_tree(decide_split):
    """`git.open_pr` into the contract tree dies at the gateway for any
    role outside the declared owners — the PR never exists, and the audit
    row names the class, not an index."""
    decision = decide_split(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={
                "branch": "feature/x",
                "paths": ["slugify.py", "tests/contract/test_api.py"],
            },
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_contract_tree"}


def test_unit_tests_stay_the_implementers(decide_split):
    """The split, not a freeze: everything outside the contract tree is
    ordinary code the implementer still owns end to end."""
    decision = decide_split(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={
                "branch": "feature/x",
                "paths": ["slugify.py", "tests/unit/test_slugify.py"],
            },
        )
    )
    assert decision["allow"] is True


def test_the_contract_author_delivers_into_the_contract_tree(decide_split):
    """The split's positive half: the contract-authoring crew's author —
    a distinct, deliver-capable template — opens the contract PR through
    the same governed `git.open_pr` as any change."""
    decision = decide_split(
        **ephemeral(
            "contract-authoring",
            "author",
            "contract-author",
            tool="git.open_pr",
            args={
                "branch": "feature/slugify-contract",
                "paths": ["tests/contract/test_slugify.py"],
            },
        )
    )
    assert decision["allow"] is True


def test_the_pm_may_request_the_contract_crew(decide_split):
    """`may_spawn` bounds the second crew like the first: granted in this
    blueprint, so the request is allowed; the ungranted-template deny is
    `test_crew_request_is_narrowed_to_may_spawn`'s, unchanged."""
    decision = decide_split(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "contract-authoring", "reason": "author contract"},
        )
    )
    assert decision["allow"] is True


def test_a_declared_owner_passes_the_contract_rule(tmp_path_factory, blueprint):
    """The rule keys on the role template — a caller keyed on what it *is*:
    a caller the declaration names is not denied by it (whether that template
    holds `git.open_pr` at all stays the allow-list's business, as ever)."""
    blueprint["policies"]["contract_tests"] = {
        "paths": ["tests/contract/"],
        "roles": ["backend-engineer"],
    }
    decision = _eval_bundle(
        tmp_path_factory,
        blueprint,
        "contract-owner",
        ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": ["tests/contract/test_api.py"]},
        ),
    )
    assert decision["allow"] is True


def test_a_contract_path_never_policy_merges(decide_split):
    """Every fact green and the resolve still denies by the class's own
    name: oracle edits never auto-merge with the code they certify — they
    park on the human `merge` signal like any carve-out."""
    decision = decide_split(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/contract/test_api.py"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_contract_path"}


def test_the_delegated_class_still_resolves_beside_the_contract_tree(decide_split):
    decision = decide_split(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/unit/test_a.py"],
        )
    )
    assert decision == {"allow": True, "rule": "merge_resolve_delegated"}


def test_a_red_contract_verify_merges_nothing(decide_split):
    """The baseline layer is required: a head suite weakened to green
    cannot launder a merge — the base-ref contract run is its own fact,
    and red denies by name."""
    decision = decide_split(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            contract_green=False,
            paths=["src/a.py"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_contract_not_green"}


def test_the_contract_fact_fails_closed_when_absent(decide_split):
    """A declared contract tree prices its fact into the delegation: the
    absent key denies, the checks_not_green idiom."""
    args = merge_resolve(verdict="pass", checks_green=True, paths=["src/a.py"])
    del args["args"]["contract_green"]
    decision = decide_split(**args)
    assert decision == {"allow": False, "rule": "merge_resolve_contract_not_green"}


def test_no_contract_tree_means_no_contract_fact(decide_delegated):
    """Vacuous only by declaration: a company without `contract_tests`
    compiled no contract rule, so the delegation reads exactly the facts
    it always did — the fact is not a new global requirement."""
    args = merge_resolve(
        verdict="pass", checks_green=True, paths=["src/a.py", "tests/test_a.py"]
    )
    del args["args"]["contract_green"]
    decision = decide_delegated(**args)
    assert decision == {"allow": True, "rule": "merge_resolve_delegated"}


# -- the contract tree's authority is not looser, and it revokes like authority


def test_the_contract_author_cannot_merge(decide_split):
    """Authority not looser: the deliver-capable contract-author template
    gains `git.open_pr` into its tree and nothing else — `git.merge_pr`
    stays platform-only."""
    as_author = decide_split(
        **ephemeral(
            "contract-authoring",
            "author",
            "contract-author",
            tool="git.merge_pr",
            args={"number": 4},
        )
    )
    assert as_author["allow"] is False
    as_platform = decide_split(
        PLATFORM_QUERY, lifecycle="platform", tool="git.merge_pr", args={"number": 4}
    )
    assert as_platform["allow"] is True


def test_deny_paths_still_bind_the_contract_author(decide_split):
    """The blueprint's deny_paths outrank every grant for the contract
    author too: owning the contract tree buys no reach into a denied surface."""
    decision = decide_split(
        **ephemeral(
            "contract-authoring",
            "author",
            "contract-author",
            tool="git.open_pr",
            args={
                "branch": "feature/x-contract",
                "paths": [".github/workflows/ci.yml", "tests/contract/test_api.py"],
            },
        )
    )
    assert decision == {"allow": False, "rule": "git_open_pr_denied_paths"}


def test_the_contract_crew_rides_the_spawn_caps(decide_split):
    """`may_spawn` caps and the budget stop bind the second crew exactly
    as the first: over the concurrency cap or without headroom, the
    contract-authoring request denies by rule — spawning the spec's
    author is governed spend like any other."""
    over = decide_split(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "contract-authoring", "reason": "author contract"},
        ),
        agent_id="pm-1",
        active_spawns=2,
        budget_headroom=True,
    )
    assert over == {"allow": False, "rule": "crew_request_over_cap"}
    broke = decide_split(
        **standing(
            "pm",
            tool="crew.request",
            args={"crew_template": "contract-authoring", "reason": "author contract"},
        ),
        agent_id="pm-1",
        active_spawns=0,
        budget_headroom=False,
    )
    assert broke == {"allow": False, "rule": "crew_request_no_budget"}


@pytest.fixture(scope="module")
def decide_revoked(tmp_path_factory):
    """`decide_split`'s company after the human revokes the contract tree:
    the same blueprint with `policies.contract_tests` deleted — one diff,
    recompiled. The crew and its spawn grant may outlive the declaration;
    the tree's special authority may not."""
    return _decider(tmp_path_factory, _company(_contract_crew))


def test_revocation_by_blueprint_diff_restores_the_single_tree(decide_revoked):
    """The contract tree is authority, so it revokes like authority: with
    the declaration gone the ownership deny is gone — the implementer
    reaches the tree again — and the delegation reads the facts of an
    undeclared company, the contract fact not priced in."""
    reopened = decide_revoked(
        **ephemeral(
            "feature-delivery",
            "executor",
            "backend-engineer",
            tool="git.open_pr",
            args={"branch": "feature/x", "paths": ["tests/contract/test_api.py"]},
        )
    )
    assert reopened["allow"] is True

    args = merge_resolve(
        verdict="pass", checks_green=True, paths=["src/a.py", "tests/unit/test_a.py"]
    )
    del args["args"]["contract_green"]
    resolved = decide_revoked(**args)
    assert resolved == {"allow": True, "rule": "merge_resolve_delegated"}


def test_revocation_leaves_the_tree_undelegated_not_unguarded(decide_revoked):
    """No looseness through the diff either: the revoked tree is ordinary
    code outside the delegated class, so its merges still park on the
    human — by class membership now, not by carve-out."""
    decision = decide_revoked(
        **merge_resolve(
            verdict="pass",
            checks_green=True,
            paths=["src/a.py", "tests/contract/test_api.py"],
        )
    )
    assert decision == {"allow": False, "rule": "merge_resolve_undelegated_path"}
