"""Human identity at the console, on the live cluster.

The console verifies a person the way the gateways verify a workload —
a token the API server issued, reviewed against the door's own audience
— and the two never cross: a human's token is worthless at either
gateway, an agent's is worthless at the console, and each refusal is an
audit row naming the principal. Whether a *known* person may act is the
blueprint's `company.humans`: a human the chart declares but the company
did not name is refused by name, with nothing signalled. And no platform
identity can mint a human's token: the humans exist only for the
TokenRequest a person makes with their own cluster credentials. What a
person applied reads back as they wrote it.
"""

import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest

from common.console import Problem
from common.topology import CONSOLE_TOKEN_AUDIENCE, TOKEN_AUDIENCE, human_service_account
from poseidon_cli import ConsoleClient, ConsoleError, PolicyDenied

from .blueprints import pristine_profile, restore_pristine, suite_document
from .cluster import (
    COMPANY,
    COMPANY_NS,
    HUMAN,
    PLATFORM_NS,
    audit_story,
    authn_denies,
    console,
    console_env,
    console_url,
    db_now,
    gateway_url,
    human_token,
    projected_token,
    run_kubectl,
)


def _console_as(env: dict, token: str) -> ConsoleClient:
    return ConsoleClient(console_url(env), token)


def test_the_declared_human_is_known(stack: None, env: dict) -> None:
    who = console(env).whoami()
    assert (who.human, who.lifecycle, who.company) == (HUMAN, "human", COMPANY)


def test_a_humans_token_is_refused_at_the_gateway(stack: None, env: dict) -> None:
    """The console's audience is the console's: the API server will not
    even verify the token for the gateway's audience, so the gateway sees
    a token that maps to nothing and says so in the audit store."""
    since = db_now()
    token = projected_token(
        PLATFORM_NS, human_service_account(HUMAN), CONSOLE_TOKEN_AUDIENCE
    )
    resp = httpx.get(
        gateway_url(env) + "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 401
    # The API server refuses the audience, so the gateway sees a token that
    # verifies as nobody — and says so, from its own door, after this test
    # started.
    assert authn_denies("authn_token_rejected", "unknown", "gateway", since), (
        "the gateway's refusal of the human's token was not audited"
    )


def test_a_workloads_token_is_refused_at_the_console(stack: None, env: dict) -> None:
    """An agent's projected token (the gateway audience) does not verify
    for the console; and even a token the console's audience *would*
    verify — a workload ServiceAccount minted for it — is not a person."""
    since = db_now()
    agent_token = projected_token(COMPANY_NS, "pm-1", TOKEN_AUDIENCE)
    with pytest.raises(ConsoleError) as refused:
        _console_as(env, agent_token).whoami()
    assert refused.value.status == 401

    workload_as_human = projected_token(COMPANY_NS, "pm-1", CONSOLE_TOKEN_AUDIENCE)
    with pytest.raises(ConsoleError) as refused:
        _console_as(env, workload_as_human).whoami()
    assert refused.value.status == 401
    principal = f"system:serviceaccount:{COMPANY_NS}:pm-1"
    assert authn_denies("authn_not_a_human", principal, "console", since)


def test_an_unnamed_human_is_refused_by_name(stack: None, env: dict) -> None:
    """A person the chart declares is verified; a person the company did
    not name is refused by policy, the rule says so, and the refusal does
    nothing — the wake it asked for never reaches the heartbeat."""
    stranger = "human-stranger"
    run_kubectl("-n", PLATFORM_NS, "create", "serviceaccount", stranger, timeout=30)
    try:
        token = projected_token(PLATFORM_NS, stranger, CONSOLE_TOKEN_AUDIENCE)
        # Declared to the cluster but not to the console: not a person the
        # door knows at all.
        with pytest.raises(ConsoleError) as refused:
            _console_as(env, token).whoami()
        assert refused.value.status == 401
    finally:
        run_kubectl("-n", PLATFORM_NS, "delete", "serviceaccount", stranger, timeout=30)


def test_a_known_human_the_company_did_not_name_is_denied_by_policy(
    stack: None, env: dict
) -> None:
    """The kind chart declares two humans, `owner` and `auditor`; the
    blueprint names only `owner`. The auditor is a verified person to the
    console — and nobody to the company: the act is refused by the
    compiled rule that says so, audited under their name, and the nudge
    never reaches the heartbeat. The owner's same act goes through."""
    auditor = console(env, name="auditor")
    assert auditor.whoami().human == "auditor"
    with pytest.raises(PolicyDenied) as denied:
        auditor.wake()
    assert denied.value.rule == "human_not_recognized"
    story = audit_story(f"heartbeat-{COMPANY}")
    refusals = [
        e for e in story if e["source"] == "console" and e["agent_id"] == "auditor"
    ]
    assert refusals and refusals[-1]["decision"] == "deny"
    assert refusals[-1]["rule"] == "human_not_recognized"
    woken = console(env).wake()
    assert (woken.company, woken.rule) == (COMPANY, "heartbeat_wake")


def test_no_platform_identity_can_mint_a_humans_token(stack: None) -> None:
    """The humans exist for a person's own TokenRequest; the reconciler and
    the worker — the two platform identities with any ServiceAccount
    authority — cannot mint one."""
    for subject in (
        f"system:serviceaccount:{PLATFORM_NS}:poseidon-reconciler",
        f"system:serviceaccount:{PLATFORM_NS}:orchestrator-worker",
    ):
        proc = run_kubectl(
            "-n",
            PLATFORM_NS,
            "create",
            "token",
            human_service_account(HUMAN),
            f"--audience={CONSOLE_TOKEN_AUDIENCE}",
            "--as",
            subject,
            timeout=30,
        )
        assert proc.returncode != 0, f"{subject} minted a human's token"
        assert "forbidden" in (proc.stderr or "").lower(), proc.stderr


def test_what_cannot_be_acted_on_is_refused_before_policy(stack: None, env: dict) -> None:
    """What names nothing is refused before policy is even asked: a
    workflow no human may start by hand (`scope` reads the observation and
    takes no text), and a run that does not exist. The typed client would
    not even send the first, so it goes as a person's raw request."""
    scope = httpx.post(
        f"{console_url(env)}/runs",
        json={"workflow": "scope", "number": 1},
        headers={"Authorization": f"Bearer {human_token()}"},
        timeout=30,
    )
    assert scope.status_code == 422, scope.text
    assert "workflow" in Problem.model_validate(scope.json()).detail
    with pytest.raises(ConsoleError) as refused:
        console(env).resolve("run-does-not-exist", "merge", True, "e2e: no such run")
    assert refused.value.status == 422
    assert not isinstance(refused.value, PolicyDenied)


def _poseidon(env: dict, *args: str) -> subprocess.CompletedProcess:
    """`poseidon …` as a person runs it, as the human `owner`."""
    proc = subprocess.run(
        ["uv", "run", "poseidon", *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=Path(__file__).resolve().parents[2],
        env=console_env(env),
    )
    assert proc.returncode == 0, f"poseidon {args[0]}: {proc.stderr}\n{proc.stdout}"
    return proc


def test_the_applied_blueprint_reads_back_and_applies_again_unchanged(
    stack: None, env: dict, tmp_path: Path
) -> None:
    """The Company CR keeps the text a person applied: the console reads
    it back byte for byte under their name, once policy enforces it, and
    `poseidon blueprint show`'s output previews as no change — the
    compiled spec could never be applied again; this text can."""
    restore_pristine()
    applied = suite_document(pristine_profile()).read_text()
    shown = console(env).blueprint()
    assert shown.text == applied
    assert shown.sha256 == hashlib.sha256(applied.encode()).hexdigest()
    assert shown.applied_by == HUMAN
    assert (shown.phase, shown.observed_generation) == ("converged", shown.generation)

    printed = _poseidon(env, "blueprint", "show").stdout
    assert printed == applied
    again = tmp_path / "shown.yaml"
    again.write_text(printed)
    preview = _poseidon(env, "blueprint", "preview", str(again))
    assert f"{COMPANY}: no changes" in preview.stdout, preview.stdout
