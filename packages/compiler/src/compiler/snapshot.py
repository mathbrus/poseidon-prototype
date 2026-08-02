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

    def instance_for(self, company: str, hire: str) -> InstanceSnapshot | None:
        for instance in self.instances:
            if instance.company == company and instance.hire == hire:
                return instance
        return None


EMPTY = RegistrySnapshot()
