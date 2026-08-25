"""The registry snapshot the compiler diffs against.

A value, not a connection: the compiler must stay a pure function, so the
caller (the CLI) reads the registry and hands the result in. That is also
what lets the compiler be tested without a database.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InstanceSnapshot:
    agent_id: str
    company: str
    standing_role: str
    role_template: str
    model_tier: str
    status: str = "ready"


@dataclass(frozen=True)
class RegistrySnapshot:
    """What the platform believes is live right now. Empty means a company
    that has never been applied."""

    instances: tuple[InstanceSnapshot, ...] = field(default_factory=tuple)

    def instances_for(
        self, company: str, standing_role: str
    ) -> tuple[InstanceSnapshot, ...]:
        """Every live instance of a standing_role, in snapshot order — one standing_role can
        have several (count > 1), each keeping its own agent_id across
        applies."""
        return tuple(
            instance
            for instance in self.instances
            if instance.company == company and instance.standing_role == standing_role
        )


EMPTY = RegistrySnapshot()
