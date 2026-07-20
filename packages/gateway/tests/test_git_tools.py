"""S6 gateway git-tool tests: repo.archive and git.open_pr.

TARGET_REPO is a local bare repo fixture (file path URL) and GitHub is an
httpx-mock stub; git subprocesses are real, the network is never touched.
"""

import base64
import io
import json
import tarfile

import pytest

from conftest import (
    AGENT_TOKEN,
    GIT_TOKEN,
    PR_URL,
    RUNNER_TOKEN,
    audit_events,
    git,
    make_bundle,
    mcp_session,
)
from gateway.git_backend import repo_slug

pytestmark = pytest.mark.anyio

RUN_ID = "run-01TESTULID"


def tool_payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def refs(repo) -> list[str]:
    return git("for-each-ref", "--format=%(refname)", cwd=repo).split()


async def open_pr(app, token, *, branch, bundle_b64, repo):
    async with mcp_session(app, token) as session:
        return await session.call_tool(
            "git.open_pr",
            {
                "run_id": RUN_ID,
                "repo": str(repo),
                "branch": branch,
                "title": "Add greeting",
                "body": "Adds a greeting function.",
                "bundle_b64": bundle_b64,
            },
        )


# --- repo.archive -------------------------------------------------------------


async def test_runner_archives_repo_stripped_of_remotes(app, target_repo, tmp_path, audit_path):
    async with mcp_session(app, RUNNER_TOKEN) as session:
        result = await session.call_tool(
            "repo.archive", {"repo": str(target_repo), "ref": "main"}
        )

    raw = base64.b64decode(tool_payload(result)["archive_b64"])
    assert GIT_TOKEN.encode() not in raw

    extracted = tmp_path / "extracted"
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        names = tar.getnames()
        tar.extractall(extracted, filter="data")
    assert "README.md" in names and "app.py" in names
    # A workspace, not a snapshot: full git history, but no remotes to leak.
    assert git("log", "--format=%s", cwd=extracted).strip() == "initial commit"
    assert git("remote", cwd=extracted).strip() == ""

    event = audit_events(audit_path)[-1]
    assert event["tool"] == "repo.archive" and event["role"] == "runner"
    assert event["decision"] == "allow" and event["rule"] == "repo_archive_runner"
    assert event["backend_status"] == "ok"


async def test_agent_token_cannot_archive(app, target_repo, audit_path):
    async with mcp_session(app, AGENT_TOKEN) as session:
        result = await session.call_tool(
            "repo.archive", {"repo": str(target_repo), "ref": "main"}
        )

    assert result.isError
    assert "default_deny" in result.content[0].text
    event = audit_events(audit_path)[-1]
    assert event["tool"] == "repo.archive" and event["decision"] == "deny"


# --- git.open_pr: happy path ----------------------------------------------------


async def test_feature_branch_pr_is_pushed_and_opened(
    app, backends, target_repo, tmp_path, audit_path
):
    bundle = make_bundle(
        target_repo, tmp_path, "feature/add-greeting", {"greeting.py": "print('hi')\n"}
    )

    result = await open_pr(
        app, AGENT_TOKEN, branch="feature/add-greeting", bundle_b64=bundle, repo=target_repo
    )

    assert tool_payload(result) == {"pr_url": PR_URL}
    # The branch reached the target repo via the platform-side push.
    assert "refs/heads/feature/add-greeting" in refs(target_repo)

    path, payload, headers = backends.github_calls[0]
    assert path == f"/repos/{repo_slug(str(target_repo))}/pulls"
    assert payload == {
        "title": "Add greeting",
        "body": "Adds a greeting function.",
        "head": "feature/add-greeting",
        "base": "main",
    }
    assert headers["authorization"] == f"Bearer {GIT_TOKEN}"

    event = audit_events(audit_path)[-1]
    assert event["tool"] == "git.open_pr" and event["run_id"] == RUN_ID
    assert event["decision"] == "allow" and event["rule"] == "git_open_pr_feature_branch"
    assert event["backend_status"] == "ok"


async def test_changed_paths_are_part_of_the_policy_input(app, backends, target_repo, tmp_path):
    bundle = make_bundle(
        target_repo,
        tmp_path,
        "feature/two-files",
        {"src/new_module.py": "x = 1\n", "docs/notes.md": "notes\n"},
    )

    await open_pr(app, AGENT_TOKEN, branch="feature/two-files", bundle_b64=bundle, repo=target_repo)

    policy_input = backends.policy_inputs[-1]
    assert policy_input["tool"] == "git.open_pr"
    assert sorted(policy_input["args"]["paths"]) == ["docs/notes.md", "src/new_module.py"]


# --- git.open_pr: denies ----------------------------------------------------------


async def test_pr_to_main_is_denied_and_nothing_is_pushed(
    app, backends, target_repo, tmp_path, audit_path
):
    before = git("rev-parse", "main", cwd=target_repo)
    bundle = make_bundle(target_repo, tmp_path, "main", {"evil.py": "pwned = True\n"})

    result = await open_pr(app, AGENT_TOKEN, branch="main", bundle_b64=bundle, repo=target_repo)

    assert result.isError
    assert "git_open_pr_protected_branch" in result.content[0].text
    assert git("rev-parse", "main", cwd=target_repo) == before
    assert backends.github_calls == []

    event = audit_events(audit_path)[-1]
    assert event["decision"] == "deny" and event["rule"] == "git_open_pr_protected_branch"
    assert event["backend_status"] is None


async def test_pr_touching_workflows_is_denied(app, backends, target_repo, tmp_path, audit_path):
    bundle = make_bundle(
        target_repo,
        tmp_path,
        "feature/sneaky-ci",
        {".github/workflows/ci.yml": "on: push\n"},
    )

    result = await open_pr(
        app, AGENT_TOKEN, branch="feature/sneaky-ci", bundle_b64=bundle, repo=target_repo
    )

    assert result.isError
    assert "git_open_pr_workflow_paths" in result.content[0].text
    assert "refs/heads/feature/sneaky-ci" not in refs(target_repo)
    assert backends.github_calls == []
    assert audit_events(audit_path)[-1]["rule"] == "git_open_pr_workflow_paths"


# --- git.open_pr: bundle tampering -------------------------------------------------


async def test_tampered_bundle_errors_and_is_audited(
    app, backends, target_repo, tmp_path, audit_path
):
    good = bytearray(base64.b64decode(make_bundle(target_repo, tmp_path, "feature/ok", {"f.py": "1\n"})))
    good[len(good) // 2] ^= 0xFF  # flip a byte in the packfile
    tampered = base64.b64encode(bytes(good)).decode()

    result = await open_pr(app, AGENT_TOKEN, branch="feature/ok", bundle_b64=tampered, repo=target_repo)

    assert result.isError
    assert "refs/heads/feature/ok" not in refs(target_repo)
    assert backends.github_calls == []

    # The attempt is audited: policy allowed the call, the backend failed on it.
    event = audit_events(audit_path)[-1]
    assert event["tool"] == "git.open_pr" and event["run_id"] == RUN_ID
    assert event["decision"] == "allow" and event["backend_status"] == "error"


async def test_garbage_bundle_errors_and_is_audited(app, backends, target_repo, audit_path):
    garbage = base64.b64encode(b"definitely not a git bundle").decode()

    result = await open_pr(app, AGENT_TOKEN, branch="feature/ok", bundle_b64=garbage, repo=target_repo)

    assert result.isError
    assert backends.github_calls == []
    event = audit_events(audit_path)[-1]
    assert event["decision"] == "allow" and event["backend_status"] == "error"
