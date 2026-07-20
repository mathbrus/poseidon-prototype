"""S8 CLI tests: the `status` command against manifests on disk."""

import pytest
from typer.testing import CliRunner

from runner.cli import app
from runner.flow import Manifest, write_manifest
from conftest import RUN_ID

runner = CliRunner()


@pytest.fixture
def env(settings, monkeypatch):
    monkeypatch.setenv("RUNNER_BEARER_TOKEN", settings.runner_bearer_token)
    monkeypatch.setenv("MCP_GATEWAY_URL", settings.mcp_gateway_url)
    monkeypatch.setenv("TARGET_REPO", settings.target_repo)
    monkeypatch.setenv("RUNS_DIR", str(settings.runs_dir))
    return settings


def test_status_prints_manifest(env):
    write_manifest(
        env,
        Manifest(
            run_id=RUN_ID,
            task="do it",
            status="done",
            created_at="2026-07-20T19:00:00+00:00",
            pr_url="https://github.test/pull/9",
            iterations=2,
            audit_lines=12,
        ),
    )

    result = runner.invoke(app, ["status", RUN_ID])

    assert result.exit_code == 0
    assert RUN_ID in result.output
    assert "https://github.test/pull/9" in result.output
    assert '"audit_lines": 12' in result.output


def test_status_unknown_run_exits_nonzero(env):
    result = runner.invoke(app, ["status", "run-GHOST"])
    assert result.exit_code == 1
