"""What the operator would apply, checked without a cluster.

The reconcilers are thin wrappers over these builders, so the properties
that matter — the isolation envelope, no credentials in a spec, the
ownership boundary — are testable here.
"""

import pytest

from poseidon_operator import resources

SPEC = {
    "agentId": "pm-1",
    "company": "hello-saas",
    "hire": "pm",
    "roleTemplate": "pm",
    "modelTier": "standard",
    "tools": ["issues.read", "crew.request"],
}


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("PLATFORM_NAMESPACE", "poseidon-platform")


def test_company_namespace_is_labelled_for_its_company():
    ns = resources.namespace("hello-saas")
    assert ns["metadata"]["labels"]["poseidon.dev/company"] == "hello-saas"


def test_default_deny_covers_every_pod_both_ways():
    spec = resources.default_deny("hello-saas")["spec"]
    assert spec["podSelector"] == {}
    assert set(spec["policyTypes"]) == {"Ingress", "Egress"}


def test_agent_egress_reaches_only_the_two_gateways_and_dns():
    egress = resources.agent_egress("hello-saas")["spec"]["egress"]
    targets = {
        rule["to"][0]["podSelector"]["matchLabels"].get("app")
        or rule["to"][0]["podSelector"]["matchLabels"].get("k8s-app")
        for rule in egress
    }
    # The model route is the metered llm-gateway; the
    # llm-proxy behind it is sealed to everyone but that gateway.
    assert targets == {"mcp-gateway", "llm-gateway", "kube-dns"}


def test_agent_ingress_admits_only_the_orchestrator():
    ingress = resources.agent_ingress("hello-saas")["spec"]["ingress"]
    (rule,) = ingress
    assert rule["from"][0]["podSelector"]["matchLabels"]["app"] == "orchestrator-worker"


def test_sandbox_answers_the_gateway_and_reaches_only_the_proxy():
    spec = resources.sandbox_isolation("hello-saas")["spec"]
    _assert_sandbox_envelope(spec)


def test_sandbox_isolation_projects_the_infra_ir():
    """The envelope comes from the compiled projection; the compiler's
    standard emission must produce the same envelope as the defaults (a CR
    carrying no projection converges identically)."""
    projected = resources.sandbox_isolation(
        "hello-saas",
        {"sandbox_ingress_from": ["mcp-gateway"], "sandbox_egress_to": ["egress-proxy"]},
    )
    assert projected == resources.sandbox_isolation("hello-saas")
    _assert_sandbox_envelope(projected["spec"])


def _assert_sandbox_envelope(spec):
    assert (
        spec["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app"]
        == "mcp-gateway"
    )
    egress_targets = {
        rule["to"][0]["podSelector"]["matchLabels"].get("app")
        or rule["to"][0]["podSelector"]["matchLabels"].get("k8s-app")
        for rule in spec["egress"]
    }
    # No kube-dns: the sandbox gets no resolver at all (see
    # sandbox_isolation) — the audited proxy is its only peer.
    assert egress_targets == {"egress-proxy"}


def test_agent_deployment_carries_no_credential_env_at_all():
    """Credential-free by construction: identity is the projected
    token file, so no token-shaped env var exists — not even a secretKeyRef.
    GPG_KEY (baked into the base image) is blanked."""
    container = resources.agent_deployment(SPEC, "hello-saas")["spec"]["template"][
        "spec"
    ]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert "AGENT_BEARER_TOKEN" not in env
    assert env["GPG_KEY"]["value"] == ""
    for name, entry in env.items():
        if name.endswith("_TOKEN") or name.endswith("_KEY"):
            assert not entry.get("value"), name
        assert "valueFrom" not in entry, name


def test_agent_pod_runs_non_root_with_no_escalation_path():
    pod_spec = resources.agent_deployment(SPEC, "hello-saas")["spec"]["template"]["spec"]
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod_spec["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def test_agent_pod_identity_is_the_audience_scoped_projected_token():
    """The pod runs as its instance's ServiceAccount and mounts one
    projected token, audience-scoped to the gateway; the API-server token
    stays unmounted, so the pod holds no cluster identity."""
    pod = resources.agent_deployment(SPEC, "hello-saas")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["serviceAccountName"] == "pm-1"
    (volume,) = pod["volumes"]
    (source,) = volume["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == resources.TOKEN_AUDIENCE
    (mount,) = pod["containers"][0]["volumeMounts"]
    assert mount["name"] == volume["name"] and mount["readOnly"] is True
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env["GATEWAY_TOKEN_FILE"] == f"{resources.TOKEN_MOUNT_DIR}/token"


def test_agent_service_account_is_named_by_agent_id():
    account = resources.agent_service_account(SPEC, "hello-saas")
    assert account["metadata"]["name"] == "pm-1"
    assert account["metadata"]["namespace"] == "hello-saas"


def test_standing_agents_are_labelled_for_the_netpol_selector():
    """The egress and ingress policies select on this label, so a Deployment
    that lost it would silently gain unrestricted egress."""
    labels = resources.agent_deployment(SPEC, "hello-saas")["spec"]["template"][
        "metadata"
    ]["labels"]
    assert labels["poseidon.dev/kind"] == "standing-agent"
    assert labels["poseidon.dev/agent-id"] == "pm-1"


def test_service_targets_the_hire():
    service = resources.agent_service(SPEC, "hello-saas")
    assert service["metadata"]["name"] == "pm"
    assert service["spec"]["selector"] == {"app": "pm"}


def test_spec_edits_change_the_pod_template():
    """The step-10 done-when: a blueprint edit (model tier, tool scope) must
    converge as a rollout, so the spec's tier and tools ride the template."""
    small = resources.agent_deployment({**SPEC, "modelTier": "small"}, "hello-saas")
    frontier = resources.agent_deployment({**SPEC, "modelTier": "frontier"}, "hello-saas")
    assert small["spec"]["template"] != frontier["spec"]["template"]

    env = {
        e["name"]: e.get("value")
        for e in small["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["MODEL_TIER"] == "small"
    assert env["ROLE_TEMPLATE"] == SPEC["roleTemplate"]
    assert env["TOOLS"] == ",".join(sorted(SPEC["tools"]))
    labels = small["spec"]["template"]["metadata"]["labels"]
    assert labels["poseidon.dev/model-tier"] == "small"


def test_sandbox_provisioner_rbac_is_bounded():
    """A review artifact: jobs and pods in the company namespace
    only, pods/exec for workspace delivery, nothing wider."""
    role = resources.sandbox_provisioner_role("hello-saas")
    resources_granted = {
        resource for rule in role["rules"] for resource in rule["resources"]
    }
    assert resources_granted == {"jobs", "pods", "pods/exec"}
    binding = resources.sandbox_provisioner_binding("hello-saas")
    (subject,) = binding["subjects"]
    assert subject["name"] == "orchestrator-worker"
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
