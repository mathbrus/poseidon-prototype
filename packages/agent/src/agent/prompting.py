"""Everything that reaches the model before its first turn: the system
prompt for a procedure, and the repository's own instructions.

The system prompt is the procedure's own text (one prose file per
procedure under `prompts/`, each opening with the `PROCEDURE_HEADER` line
that names it — the one line the mock brain keys on), followed by the
coding heuristics
the OpenHands SDK ships as prompt sections. The SDK's default prompt body
is deliberately not used: it addresses a user who is in the loop, gives
the model an OpenHands identity, assumes a networked machine that
installs its own dependencies, and tells the model to keep notes in a
writable AGENTS.md — none of which exist on a brain host. What is reused
is the handful of sections that say nothing about who the model is or
which tools it holds — efficiency, file hygiene, code quality, the
per-model-family guidance — assembled by the SDK's own registry, so a
version bump updates the text.

The repository's AGENTS.md, when the sandbox has one, is handed to the
SDK as a repo skill: it renders in the system message's second content
block inside the SDK's UNTRUSTED_CONTENT framing, which is exactly what
text from the repository is, and the static prompt stays byte-identical.
"""

from pathlib import Path

from openhands.sdk.context import AgentContext
from openhands.sdk.context.prompts.registry import PromptRegistry
from openhands.sdk.context.prompts.section import PromptContext, PromptSection
from openhands.sdk.context.prompts.sections.static import (
    CodeQualitySection,
    EfficiencySection,
    FileSystemSection,
    ModelSpecificSection,
)
from openhands.sdk.llm.utils.model_prompt_spec import get_model_prompt_spec
from openhands.sdk.skills import Skill

from agent.config import AgentSettings

PROMPTS_DIR = Path(__file__).parent / "prompts"
# The first line of every prompt file: `# Poseidon procedure: <name>`.
PROCEDURE_HEADER = "# Poseidon procedure: "

# Which sections follow the procedure's text: a procedure that writes
# code gets them all, one that only reads gets efficiency, one that never
# sees a workspace gets the model guidance alone. The model-family
# guidance renders nothing for a model the SDK does not recognise (the
# mock).
_CODING: tuple[type[PromptSection], ...] = (
    EfficiencySection,
    FileSystemSection,
    CodeQualitySection,
    ModelSpecificSection,
)
_READING: tuple[type[PromptSection], ...] = (EfficiencySection, ModelSpecificSection)
_MODEL: tuple[type[PromptSection], ...] = (ModelSpecificSection,)
SECTIONS: dict[str, tuple[type[PromptSection], ...]] = {
    "scope": _MODEL,
    "decide": _MODEL,
    "implement": _CODING,
    "deliver": _CODING,
    "review": _READING,
}

# The repository's instructions to agents, shown as untrusted context.
# Bounded: a file can be anything.
REPO_INSTRUCTIONS_FILE = "AGENTS.md"
REPO_INSTRUCTIONS_CLIP = 8000


def render_procedure(procedure: str, settings: AgentSettings) -> str:
    """The procedure's prompt file, with literal placeholder substitution —
    deliberately not str.format: format machinery turns any other brace in
    the prompt text into a crash (or a format-spec evaluation), and prompt
    files are edited as prose. The header line is checked, not trusted: a
    file that names another procedure is a packaging error."""
    text = (PROMPTS_DIR / f"{procedure}.md").read_text()
    if not text.startswith(f"{PROCEDURE_HEADER}{procedure}\n"):
        raise RuntimeError(f"prompts/{procedure}.md does not open with its header")
    return text.replace("{target_repo}", settings.target_repo)


def compose(procedure: str, sections: tuple[type[PromptSection], ...], model: str) -> str:
    """The procedure first — it says what this dispatch is — then every
    section that applies to the model, in the SDK's own words."""
    registry = PromptRegistry()
    for section in sections:
        registry.register(section())
    spec = get_model_prompt_spec(model)
    ctx = PromptContext(
        template_kwargs={"model_family": spec.family, "model_variant": spec.variant}
    )
    heuristics = registry.build(ctx).static.strip()
    return procedure.rstrip() + (f"\n\n{heuristics}" if heuristics else "") + "\n"


def system_prompt(procedure: str, settings: AgentSettings, model: str) -> str:
    return compose(render_procedure(procedure, settings), SECTIONS[procedure], model)


def repo_instructions(text: str | None) -> AgentContext | None:
    """The sandbox's AGENTS.md as the conversation's agent context, or
    None when there is none to show."""
    if text is None:
        return None
    if len(text) > REPO_INSTRUCTIONS_CLIP:
        dropped = len(text) - REPO_INSTRUCTIONS_CLIP
        text = text[:REPO_INSTRUCTIONS_CLIP] + f"\n[... {dropped} chars clipped ...]\n"
    return AgentContext(
        skills=[Skill(name=REPO_INSTRUCTIONS_FILE, content=text)],
        current_datetime=None,
    )
