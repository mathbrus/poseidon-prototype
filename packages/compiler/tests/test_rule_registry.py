"""The rule registry in `common.policy` and the compiled bundles agree.

The reference docs render the registry as the list of every rule a
decision can answer with; these tests are what keeps that list true.
"""

import re

import pytest
import yaml
from conftest import REPO_ROOT

from common.policy import RULES
from compiler import compile_blueprint

EXAMPLES = sorted((REPO_ROOT / "examples").glob("*.yaml"))
EMITTED = re.compile(r'(?:allow|deny)_rules contains "([a-z0-9_]+)"')
# Supplied by the enforcement points, never emitted: the bundle's default
# decision, and the answer when OPA serves no document at all.
NOT_EMITTED = {"default_deny", "no_policy_installed"}
# The always-false rule that keeps an empty rule set loadable.
PLACEHOLDER = "declared_nowhere"


def _pattern(name: str) -> re.Pattern[str]:
    """`role_<standing>_<tool>` as a regular expression."""
    return re.compile("^" + re.sub(r"<[a-z]+>", "[a-z0-9_]+", re.escape(name)) + "$")


def _emitted() -> dict[str, str]:
    """Every rule name the shipped examples compile to, with its effect."""
    found: dict[str, str] = {}
    for path in EXAMPLES:
        plan = compile_blueprint(yaml.safe_load(path.read_text()))
        for text in {**plan.bundle.files, **plan.platform_bundle.files}.values():
            for match in EMITTED.finditer(text):
                effect = "allow" if match.group(0).startswith("allow") else "deny"
                found[match.group(1)] = effect
    found.pop(PLACEHOLDER, None)
    return found


@pytest.fixture(scope="module")
def emitted() -> dict[str, str]:
    return _emitted()


def test_every_emitted_rule_is_registered_with_its_effect(emitted):
    literal = {r.name: r for r in RULES if "<" not in r.name}
    patterned = [r for r in RULES if "<" in r.name]
    for name, effect in emitted.items():
        rule = literal.get(name) or next(
            (r for r in patterned if _pattern(r.name).match(name)), None
        )
        assert rule is not None, f"{name} is emitted but not in common.policy.RULES"
        assert rule.effect == effect, (
            f"{name} is emitted as {effect}, registered as {rule.effect}"
        )


def test_every_registered_rule_is_emitted_by_some_example(emitted):
    literal_names = {r.name for r in RULES if "<" not in r.name}
    for rule in RULES:
        if rule.name in NOT_EMITTED:
            continue
        if "<" in rule.name:
            candidates = [n for n in emitted if n not in literal_names]
            assert any(_pattern(rule.name).match(n) for n in candidates), rule.name
        else:
            assert rule.name in emitted, f"{rule.name} is registered but never emitted"
