"""Preview: what applying this plan would change, and what it would cost.

`poseidon blueprint preview` exists so a standing_role, a fire or a scope change is
readable *before* it converges — the declarative loop is only trustworthy if
you can see the diff first.
"""

from dataclasses import dataclass, field

from compiler.plan import DeploymentPlan
from compiler.snapshot import RegistrySnapshot


@dataclass
class InstanceChange:
    # Keyed by agent_id, not standing_role: with count > 1 a standing_role is several
    # instances, and a count change must show up as creates/deletes.
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
                InstanceChange(
                    agent_id, "create", (f"template {instance.role_template}",)
                )
            )
            continue
        details = []
        if existing.role_template != instance.role_template:
            details.append(
                f"template {existing.role_template} -> {instance.role_template}"
            )
        if existing.model_tier != instance.model_tier:
            details.append(f"model_tier {existing.model_tier} -> {instance.model_tier}")
        if details:
            result.changes.append(InstanceChange(agent_id, "update", tuple(details)))

    for agent_id in live:
        if agent_id not in planned:
            # The firing half of hire/fire-as-diff; the operator drains
            # before removing.
            result.changes.append(
                InstanceChange(agent_id, "delete", ("no longer hired",))
            )

    result.changes.sort(key=lambda c: (c.action, c.agent_id))
    return result
