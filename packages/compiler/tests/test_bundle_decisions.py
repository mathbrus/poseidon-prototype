"""The compiled bundle, evaluated by the real policy engine.

Golden files pin what we emit; these pin what it *decides* — which is the
part that matters. Every case here is an exit criterion or a threat from the
overview, so a refactor of the emitter that quietly widens a scope fails
here rather than in production.

Needs the `opa` binary or Docker; skips when neither is present so a laptop
without them still gets a green run (CI has Docker).
"""

import json
import shutil
import subprocess

import pytest

from compiler import compile_blueprint

# Per-company packages; the platform scope lives in its
# own bundle. Both are loaded together here — their packages (and therefore
# bundle roots) are disjoint by construction.
COMPANY_QUERY = "data.poseidon.authz.hello_saas.decision"
PLATFORM_QUERY = "data.poseidon.platform.decision"


def _runner():
    if shutil.which("opa"):
        return lambda bundle, stdin, query: subprocess.run(
            ["opa", "eval", "-d", bundle, "-f", "raw", "-I", query],
            input=stdin, capture_output=True, text=True, timeout=60,
        )
    if shutil.which("docker"):
        return lambda bundle, stdin, query: subprocess.run(
            ["docker", "run", "--rm", "-i", "-v", f"{bundle}:/b:ro",
             "openpolicyagent/opa", "eval", "-d", "/b", "-f", "raw", "-I", query],
            input=stdin, capture_output=True, text=True, timeout=120,
        )
    return None


@pytest.fixture(scope="module")
def decide(tmp_path_factory, request):
    runner = _runner()
    if runner is None:
        pytest.skip("neither the opa binary nor docker is available")
    import yaml

    from conftest import HELLO_SAAS

    blueprint = yaml.safe_load(HELLO_SAAS.read_text())
    plan = compile_blueprint(blueprint)
    bundle = tmp_path_factory.mktemp("bundle")
    for name, body in {**plan.bundle.files, **plan.platform_bundle.files}.items():
        (bundle / name).write_text(body)

    def run(_query: str = COMPANY_QUERY, **input_fields) -> dict:
        proc = runner(str(bundle), json.dumps(input_fields), _query)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return run


def ephemeral(crew: str, member: str, template: str, **extra) -> dict:
    return {
        "company": "hello-saas",
        "lifecycle": "ephemeral",
        "crew_template": crew,
        "crew_role": member,
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


# -- the lean-roster exit criteria ----------------------------------------


def test_pm_may_request_a_crew(decide):
    decision = decide(**standing("pm", tool="crew.request",
                                 args={"crew_template": "feature-review",
                                       "reason": "review PR"}))
    assert decision["allow"] is True


def test_pm_cannot_call_executor_tools(decide):
    """'The PM cannot call executor-only tools' — a gateway deny,
    not prompt hope."""
    for tool in ("git.open_pr", "sandbox.write_file", "sandbox.exec"):
        decision = decide(**standing("pm", tool=tool, args={"branch": "feature/x"}))
        assert decision["allow"] is False, tool


# -- crew members are distinct principals ---------------------------------


def test_planner_is_read_only(decide):
    """Both members run the `backend-engineer` template, so without the
    crew-member discriminator the planner would inherit delivery rights."""
    read = decide(
        **ephemeral("feature-delivery", "planner", "backend-engineer",
                    tool="sandbox.read_file", run_binding_verified=True, args={})
    )
    write = decide(
        **ephemeral("feature-delivery", "planner", "backend-engineer",
                    tool="sandbox.write_file", run_binding_verified=True, args={})
    )
    deliver = decide(
        **ephemeral("feature-delivery", "planner", "backend-engineer",
                    tool="git.open_pr", args={"branch": "feature/x", "paths": []})
    )
    assert read["allow"] is True
    assert write["allow"] is False
    assert deliver["allow"] is False


def test_executor_delivers_on_feature_branches(decide):
    decision = decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="git.open_pr",
                    args={"branch": "feature/slugify", "paths": ["slugify.py"]})
    )
    assert decision["allow"] is True


def test_reviewer_cannot_deliver(decide):
    decision = decide(
        **ephemeral("feature-review", "reviewer", "sdet",
                    tool="git.open_pr", args={"branch": "feature/x", "paths": []})
    )
    assert decision["allow"] is False


# -- the blueprint's own policies ------------------------------------------


def test_protected_branch_is_denied(decide):
    decision = decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="git.open_pr", args={"branch": "main", "paths": ["a.py"]})
    )
    assert decision["allow"] is False
    assert decision["rule"].startswith("git_open_pr_")


def test_denied_paths_beat_an_allowed_branch(decide):
    decision = decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="git.open_pr",
                    args={"branch": "feature/x", "paths": [".github/workflows/ci.yml"]})
    )
    assert decision == {"allow": False, "rule": "git_open_pr_denied_paths"}


# -- run binding and tenancy ----------------------------------------------


def test_sandbox_tools_require_a_verified_run_binding(decide):
    """A per-role allow-list without run binding reopens cross-run tool
    use; the gateway's registry enrichment is what sets this flag."""
    decision = decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="sandbox.exec", args={})
    )
    assert decision["allow"] is False


def test_another_company_cannot_borrow_our_rules(decide):
    decision = decide(
        company="evil-corp", lifecycle="standing", role="pm",
        tool="crew.request", run_id="run-1", args={},
    )
    assert decision["allow"] is False


# -- the platform lifecycle -----------------------------------------------


def test_only_the_platform_may_merge(decide):
    """The workflow merges under its own identity after the human
    gate — via its own decision path; no company role can reach the tool in
    the company package at all."""
    assert decide(PLATFORM_QUERY, lifecycle="platform", tool="git.merge_pr",
                  run_id="run-1", args={})["allow"] is True
    assert decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="git.merge_pr", args={})
    )["allow"] is False


def test_unknown_tool_is_denied_by_default(decide):
    decision = decide(**standing("pm", tool="issues.purge", args={}))
    assert decision == {"allow": False, "rule": "default_deny"}


def test_platform_may_retire_a_sandbox_registration(decide):
    """Both gateway admin routes are in PLATFORM_SCOPE — the DELETE was
    missing, which would have killed teardown at the step-12 cutover."""
    for tool in ("/admin/sandboxes", "/admin/sandboxes.delete"):
        assert decide(PLATFORM_QUERY, lifecycle="platform", tool=tool,
                      run_id="run-1", args={})["allow"] is True, tool


def test_company_lifecycles_find_nothing_in_the_platform_package(decide):
    """The platform package carries only platform rules: a standing or
    ephemeral caller routed there by mistake still gets default-deny."""
    assert decide(
        PLATFORM_QUERY, **standing("backend-engineer", tool="sandbox.exec",
                                   run_binding_verified=True, args={})
    ) == {"allow": False, "rule": "default_deny"}


def test_open_pr_without_paths_is_denied(decide):
    """Path rules fail closed: a git.open_pr whose input carries no
    args.paths cannot be checked against them, so it is denied (the
    gateway's bundle-apply always supplies the paths)."""
    decision = decide(
        **ephemeral("feature-delivery", "executor", "backend-engineer",
                    tool="git.open_pr", args={"branch": "feature/x"})
    )
    assert decision == {"allow": False, "rule": "git_open_pr_paths_missing"}


# -- emitter edge cases (fresh bundles, not the module fixture) -------------


def _eval_bundle(tmp_path_factory, blueprint, name, input_fields) -> dict:
    runner = _runner()
    if runner is None:
        pytest.skip("neither the opa binary nor docker is available")
    plan = compile_blueprint(blueprint)
    bundle = tmp_path_factory.mktemp(name)
    for file_name, body in plan.bundle.files.items():
        (bundle / file_name).write_text(body)
    proc = runner(str(bundle), json.dumps(input_fields), COMPANY_QUERY)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_bundle_without_policies_still_loads(tmp_path_factory, blueprint):
    """`policies` is optional; a blueprint without it used to emit a bundle
    OPA refused to load (`deny_rules` unsafe var) — the entrypoint now
    declares both rule sets."""
    del blueprint["policies"]
    decision = _eval_bundle(
        tmp_path_factory, blueprint, "no-policies",
        standing("pm", tool="issues.purge", args={}),
    )
    assert decision == {"allow": False, "rule": "default_deny"}


def test_pattern_with_a_backtick_cannot_inject_policy(tmp_path_factory, blueprint):
    """The allowed_pattern is emitted as a JSON-escaped string; a backtick
    used to terminate the raw literal and splice text into the bundle."""
    blueprint["policies"]["branch_protection"]["allowed_pattern"] = "^feature/`x.*"
    decision = _eval_bundle(
        tmp_path_factory, blueprint, "backtick-pattern",
        ephemeral("feature-delivery", "executor", "backend-engineer",
                  tool="git.open_pr",
                  args={"branch": "feature/nope", "paths": ["a.py"]}),
    )
    assert decision == {"allow": False, "rule": "git_open_pr_branch_pattern"}


def test_uppercase_protected_branch_still_protects(tmp_path_factory, blueprint):
    """`protected: [Main]` must deny `main`: branches are lowercased at emit
    time to match the lowercased input."""
    blueprint["policies"]["branch_protection"] = {"protected": ["Main"]}
    decision = _eval_bundle(
        tmp_path_factory, blueprint, "uppercase-protected",
        ephemeral("feature-delivery", "executor", "backend-engineer",
                  tool="git.open_pr", args={"branch": "main", "paths": ["a.py"]}),
    )
    assert decision == {"allow": False, "rule": "git_open_pr_protected_branch"}


def test_crew_rule_compares_the_raw_member_name():
    """`run_participants.crew_role` stores the member name as written; only
    rule *names* are slugged. A slugged comparison silently disabled every
    rule of a hyphenated member."""
    from compiler.plan import CrewMemberSpec
    from compiler.rego import build_bundle

    files = build_bundle(
        company="c",
        instances=[],
        crew_members=[
            CrewMemberSpec(
                crew="feature-review",
                member="code-reviewer",
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
    """Plan step 15: the compiled crew.request rule requires the requested
    template to be in the conductor's may_spawn list — spawn intent outside
    it never even records."""
    allowed = decide(**standing("pm", tool="crew.request",
                                args={"crew_template": "feature-delivery",
                                      "reason": "ticket"}))
    assert allowed["allow"] is True

    outside = decide(**standing("pm", tool="crew.request",
                                args={"crew_template": "shadow-ops",
                                      "reason": "hm"}))
    assert outside == {"allow": False, "rule": "default_deny"}

    absent = decide(**standing("pm", tool="crew.request", args={}))
    assert absent["allow"] is False


def test_crew_request_concurrency_cap_denies_by_rule(decide):
    """16d: the PM's max_concurrent (2 in hello-saas) is a compiled deny
    keyed on the instance — a first-class governance event, not a
    default-deny shrug. The count rides the enriched input."""
    under = decide(**standing("pm", tool="crew.request",
                              args={"crew_template": "feature-delivery"}),
                   agent_id="pm-1", active_spawns=1, budget_headroom=True)
    assert under["allow"] is True

    over = decide(**standing("pm", tool="crew.request",
                             args={"crew_template": "feature-delivery"}),
                  agent_id="pm-1", active_spawns=2, budget_headroom=True)
    assert over == {"allow": False, "rule": "crew_request_over_cap"}


def test_crew_request_without_budget_headroom_is_denied(decide):
    decision = decide(**standing("pm", tool="crew.request",
                                 args={"crew_template": "feature-delivery"}),
                      agent_id="pm-1", active_spawns=0, budget_headroom=False)
    assert decision == {"allow": False, "rule": "crew_request_no_budget"}
