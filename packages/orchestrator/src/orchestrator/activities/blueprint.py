"""Reading a workflow's shape out of the applied blueprint."""

from typing import Any

from registry import AgentInstances, Companies


def workflow_config(
    companies: Companies, instances: AgentInstances, company: str, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The named workflow's block from the company's applied blueprint,
    and the standing instance of its conductor as `{company, crew_template,
    conductor, conductor_role}` — the identity a crew request is recorded
    under. Fail-closed: no applied blueprint, no enabled workflow, no crew,
    or no converged conductor instance each refuse the run before anything
    is provisioned."""
    spec = companies.get(company) or {}
    config = next((w for w in spec.get("workflows") or [] if w.get("name") == name), None)
    if config is None or not config.get("enabled", False):
        raise RuntimeError(
            f"company {company!r} has no enabled {name} workflow in its applied blueprint"
        )
    crew_template = config.get("crew")
    if not crew_template:
        raise RuntimeError(
            f"{name} workflow binds no crew — execution is ephemeral crews;"
            " the compiler should have refused this blueprint"
        )
    conductor_role = config["conductor"]
    instance = next(
        (
            i
            for i in instances.for_company(company)
            if i.get("standing_role") == conductor_role
        ),
        None,
    )
    if instance is None:
        raise RuntimeError(
            f"no standing instance for conductor standing_role {conductor_role!r}"
            " — is the company converged?"
        )
    return config, {
        "company": company,
        "crew_template": crew_template,
        "conductor": instance["agent_id"],
        "conductor_role": instance["role_template"],
    }
