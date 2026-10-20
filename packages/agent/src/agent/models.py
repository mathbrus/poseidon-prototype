"""The model table: which wire shape and which reasoning knob a model
gets, by family.

The OpenHands SDK guesses both from LiteLLM's metadata for the model and,
failing that, from its own substring lists. Both lag the providers: a
release in a family the pinned LiteLLM has not heard of is guessed wrong,
and the first model call of every run fails at the provider ("use
/v1/responses or set reasoning_effort to none"). This table is the
deployment's own answer, keyed by family rather than by release, so a new
release in a known family needs no edit here. A model no row names keeps
the SDK's guess — the mock brain, for one.

Two knobs are pinned per family: the wire shape (`api_mode`) and whether
the model takes a reasoning effort. Poseidon speaks that one reasoning
knob, `reasoning_effort`, and LiteLLM translates it per provider; nothing
here builds a provider's thinking payload by hand. Everything else —
prompt caching, thinking style, sampling — the SDK keeps reading from
LiteLLM's metadata. The real-LLM guide renders this table; the examples
are its unit test's cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ApiMode = Literal["chat", "responses"]


@dataclass(frozen=True)
class ModelFamily:
    name: str
    # Matched at the start of the bare model name (provider prefix
    # stripped, lower-cased); the family's version digits, then a
    # separator or the end, so `gpt-5` never claims `gpt-50`.
    pattern: str
    api_mode: ApiMode
    reasoning_effort: bool
    examples: tuple[str, ...]

    def matches(self, bare_model: str) -> bool:
        return re.match(self.pattern, bare_model) is not None

    @property
    def capability_overrides(self) -> dict[str, bool]:
        """The SDK's explicit capability knobs this row pins; they beat
        LiteLLM's metadata and the SDK's fallbacks."""
        return {
            "supports_responses_api": self.api_mode == "responses",
            "supports_reasoning_effort": self.reasoning_effort,
        }


MODEL_FAMILIES: tuple[ModelFamily, ...] = (
    ModelFamily(
        name="OpenAI GPT-5 and later",
        pattern=r"gpt-(?:[5-9]|\d{2})(?:[.-]|$)",
        api_mode="responses",
        reasoning_effort=True,
        examples=("gpt-5.6-luna", "gpt-5.4-mini", "gpt-6-luna", "gpt-6-sol"),
    ),
    ModelFamily(
        name="OpenAI GPT-4 family",
        pattern=r"gpt-4(?:[.o-]|$)",
        api_mode="responses",
        reasoning_effort=False,
        examples=("gpt-4.1", "gpt-4o", "gpt-4.5"),
    ),
    ModelFamily(
        name="Anthropic Claude 4 and later",
        pattern=r"claude-(?:opus|sonnet|haiku|fable)-(?:[4-9]|\d{2})(?:[.-]|$)",
        api_mode="chat",
        reasoning_effort=True,
        examples=(
            "claude-sonnet-4-5",
            "claude-opus-4-6",
            "claude-haiku-5-5",
            "claude-opus-5-5",
            "claude-fable-5-1",
        ),
    ),
    ModelFamily(
        name="Anthropic Claude 3",
        pattern=r"claude-3(?:[.-]|$)",
        api_mode="chat",
        reasoning_effort=False,
        examples=("claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219"),
    ),
)


def bare_name(model: str) -> str:
    """`openai/gpt-6-luna` and `GPT-6-Luna` are the same model."""
    return model.rsplit("/", 1)[-1].strip().lower()


def family_of(model: str) -> ModelFamily | None:
    bare = bare_name(model)
    return next((f for f in MODEL_FAMILIES if f.matches(bare)), None)
