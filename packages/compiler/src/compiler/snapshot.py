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
    hire: str
    role_template: str
    model_tier: str
    status: str = "ready"


@dataclass(frozen=True)
class RegistrySnapshot:
    """What the platform believes is live right now. Empty means a company
    that has never been applied."""

    instances: tuple[InstanceSnapshot, ...] = field(default_factory=tuple)

    def instances_for(self, company: str, hire: str) -> tuple[InstanceSnapshot, ...]:
        """Every live instance of a hire, in snapshot order — one hire can
        have several (count > 1), each keeping its own agent_id across
        applies."""
        return tuple(
            instance
            for instance in self.instances
            if instance.company == company and instance.hire == hire
        )


EMPTY = RegistrySnapshot()
