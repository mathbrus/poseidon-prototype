"""System-prompt composition: Poseidon's operating procedure for one
dispatch mode, followed by the coding heuristics the OpenHands SDK ships
as prompt sections.

The SDK's default prompt body is deliberately not used. It addresses a
user who is in the loop, gives the model an OpenHands identity, assumes a
networked machine that installs its own dependencies, and tells the model
to keep notes in a writable AGENTS.md — none of which exist on a brain
host. What is reused is the handful of sections that say nothing about
who the model is or which tools it holds: efficiency, file hygiene, code
quality, and the per-model-family guidance. They are rendered by the
SDK's own section classes, so a version bump updates the text.
"""

from collections.abc import Iterable
from typing import Protocol

from openhands.sdk.context.prompts.section import PromptContext
from openhands.sdk.context.prompts.sections.static import (
    CodeQualitySection,
    EfficiencySection,
    FileSystemSection,
    ModelSpecificSection,
)
from openhands.sdk.llm.utils.model_prompt_spec import get_model_prompt_spec


class Section(Protocol):
    def guard(self, ctx: PromptContext) -> bool: ...
    def render(self, ctx: PromptContext) -> str | None: ...


# Sections for a dispatch that writes code, one that only reads it, and
# one that never sees a workspace at all. The model-family guidance
# renders nothing for a model the SDK does not recognise (the mock).
CODING_SECTIONS: tuple[type[Section], ...] = (
    EfficiencySection,
    FileSystemSection,
    CodeQualitySection,
    ModelSpecificSection,
)
READING_SECTIONS: tuple[type[Section], ...] = (EfficiencySection, ModelSpecificSection)
MODEL_SECTIONS: tuple[type[Section], ...] = (ModelSpecificSection,)


def prompt_context(model: str) -> PromptContext:
    spec = get_model_prompt_spec(model)
    kwargs: dict[str, object] = {}
    if spec.family:
        kwargs["model_family"] = spec.family
    if spec.variant:
        kwargs["model_variant"] = spec.variant
    return PromptContext(template_kwargs=kwargs)


def compose(procedure: str, sections: Iterable[type[Section]], model: str) -> str:
    """The procedure first — it says what this dispatch is — then every
    section that applies to the model, each in the SDK's own words."""
    ctx = prompt_context(model)
    blocks = [procedure.rstrip()]
    for section_type in sections:
        section = section_type()
        if not section.guard(ctx):
            continue
        text = section.render(ctx)
        if text and text.strip():
            blocks.append(text.strip())
    return "\n\n".join(blocks) + "\n"
