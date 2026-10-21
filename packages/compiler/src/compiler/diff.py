"""Preview: what applying this plan would change, and what it would cost.

`poseidon blueprint preview` exists so a hire, a fire or a scope change is
readable *before* it converges — the declarative loop is only trustworthy if
you can see the diff first.
"""

from dataclasses import dataclass, field

from compiler.plan import DeploymentPlan
from compiler.snapshot import RegistrySnapshot


@dataclass
class InstanceChange:
    # Keyed by agent_id, not the hire's name: with count > 1 one hire is
    # several instances, and a count change must show up as creates/deletes.
    agent_id: str
    action: str  # create | update | delete
    details: tuple[str, ...] = ()


@dataclass
class PlanDiff:
    company: str
    changes: list[InstanceChange] = field(default_factory=list)
    per_run_usd: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.changes

    def as_text(self) -> str:
        if self.empty:
            head = f"{self.company}: no changes"
        else:
            lines = [f"{self.company}: {len(self.changes)} change(s)"]
            for change in self.changes:
                mark = {"create": "+", "update": "~", "delete": "-"}[change.action]
                lines.append(f"  {mark} {change.agent_id}")
                lines.extend(f"      {detail}" for detail in change.details)
            head = "\n".join(lines)
        return f"{head}\n  budget: ${self.per_run_usd:g}/run"


def diff_plan(plan: DeploymentPlan, snapshot: RegistrySnapshot) -> PlanDiff:
    company = plan.company.name
    result = PlanDiff(
        company=company,
        per_run_usd=plan.budgets.per_run_usd,
    )

    planned = {instance.agent_id: instance for instance in plan.instances}
    live = {
        instance.agent_id: instance
        for instance in snapshot.instances
        if instance.company == company
    }

    for agent_id, instance in planned.items():
        existing = live.get(agent_id)
        if existing is None:
            result.changes.append(
                InstanceChange(agent_id, "create", (f"role {instance.role}",))
            )
            continue
        details = []
        if existing.role != instance.role:
            details.append(f"role {existing.role} -> {instance.role}")
        if details:
            result.changes.append(InstanceChange(agent_id, "update", tuple(details)))

    for agent_id in live:
        if agent_id not in planned:
            # The firing half of hire/fire-as-diff; the reconciler drains
            # before removing.
            result.changes.append(
                InstanceChange(agent_id, "delete", ("no longer hired",))
            )

    result.changes.sort(key=lambda c: (c.action, c.agent_id))
    return result
