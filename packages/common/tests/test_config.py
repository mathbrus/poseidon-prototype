from common.config import Settings

C7_VARS = [
    "GIT_TOKEN",
    "LLM_API_KEY",
    "TARGET_REPO",
    "BASE_REF",
    "MODEL",
    "MAX_ITERATIONS",
    "AGENT_BEARER_TOKEN",
    "RUNNER_BEARER_TOKEN",
    "MCP_GATEWAY_URL",
    "LLM_PROXY_URL",
]


def test_reads_all_c7_env_vars(monkeypatch):
    for var in C7_VARS:
        monkeypatch.setenv(var, f"value-of-{var.lower()}")
    monkeypatch.setenv("MAX_ITERATIONS", "5")

    s = Settings()
    assert s.git_token == "value-of-git_token"
    assert s.llm_api_key == "value-of-llm_api_key"
    assert s.target_repo == "value-of-target_repo"
    assert s.base_ref == "value-of-base_ref"
    assert s.model == "value-of-model"
    assert s.max_iterations == 5
    assert s.agent_bearer_token == "value-of-agent_bearer_token"
    assert s.runner_bearer_token == "value-of-runner_bearer_token"
    assert s.mcp_gateway_url == "value-of-mcp_gateway_url"
    assert s.llm_proxy_url == "value-of-llm_proxy_url"


def test_defaults_when_unset(monkeypatch):
    for var in C7_VARS:
        monkeypatch.delenv(var, raising=False)

    s = Settings()
    assert s.git_token is None
    assert s.base_ref == "main"
    assert s.max_iterations == 10
