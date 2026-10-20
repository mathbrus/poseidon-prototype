"""The model table decides the wire shape by family, and `build_llm`
carries its answer to the SDK."""

import pytest

from agent.models import MODEL_FAMILIES, family_of
from agent.procedures import build_llm

CASES = [(example, family) for family in MODEL_FAMILIES for example in family.examples]


@pytest.mark.parametrize("example,family", CASES, ids=[c[0] for c in CASES])
def test_every_example_names_its_own_family_and_no_other(example, family):
    assert family_of(example) is family
    assert [f for f in MODEL_FAMILIES if f.matches(example)] == [family]


def test_provider_prefix_and_case_do_not_matter():
    assert family_of("openai/GPT-6-Luna") is family_of("gpt-6-luna")
    assert family_of("anthropic/claude-opus-5-5") is family_of("claude-opus-5-5")


def test_a_family_claims_its_versions_only():
    gpt5_plus = family_of("gpt-6-luna")
    assert family_of("gpt-5") is gpt5_plus
    assert family_of("gpt-12-sol") is gpt5_plus  # two-digit majors stay in the family
    assert family_of("gpt-4.1") is not gpt5_plus
    assert family_of("gpt-5x") is None  # not a version separator
    assert family_of("claude-opus-4") is family_of("claude-fable-5-1")
    assert family_of("claude-3-opus-20240229") is not family_of("claude-opus-4")


@pytest.mark.parametrize("model", ["mock-brain", "test-model", "gemini-3-pro"])
def test_a_model_no_row_names_keeps_the_sdk_guess(settings, model):
    assert family_of(model) is None
    llm = build_llm(settings.model_copy(update={"model": model}), "run-1", "tok")
    assert llm.api_mode == "auto"
    assert llm.capability_overrides == {}


def test_gpt6_goes_over_responses_with_a_reasoning_effort(settings):
    llm = build_llm(settings.model_copy(update={"model": "gpt-6-luna"}), "run-1", "tok")
    assert llm.api_mode == "responses"
    assert llm.uses_responses_api()
    assert llm.capability_overrides == {
        "supports_responses_api": True,
        "supports_reasoning_effort": True,
    }
    assert llm.reasoning_effort  # the SDK's default effort rides along


def test_claude_stays_on_chat_completions_with_a_reasoning_effort(settings):
    llm = build_llm(
        settings.model_copy(update={"model": "anthropic/claude-opus-5-5"}), "run-1", "tok"
    )
    assert llm.api_mode == "chat"
    assert not llm.uses_responses_api()
    assert llm.capability_overrides["supports_reasoning_effort"] is True
    assert llm.model == "anthropic/claude-opus-5-5"  # the provider prefix routes it
