"""Reading a workflow's shape, and its conductor, out of the applied
blueprint and the converged standing instances."""

from typing import Any

from registry import AgentInstances, Companies

AGENT_PORT = 8000


def conductor(
    instances: AgentInstances, company: str, standing_role: str
) -> dict[str, Any]:
    """The converged standing instance behind a blueprint's conductor name,
    as `{agent_id, role, url}`: the identity a crew request is recorded
    under and the address a dispatch reaches it at. The operator names
    the instance's Service after the standing role in the company
    namespace, so the address is derived, never configured. Fail-closed:
    no converged instance refuses the run before anything is provisioned."""
    instance = next(
        (
            i
            for i in instances.for_company(company)
            if i.get("standing_role") == standing_role
        ),
        None,
    )
    if instance is None:
        raise RuntimeError(
            f"no standing instance for conductor standing_role {standing_role!r}"
            " — is the company converged?"
        )
    return {
        "agent_id": instance["agent_id"],
        "role": instance["role_template"],
        "url": f"http://{standing_role}.{instance['namespace']}.svc.cluster.local:{AGENT_PORT}",
    }


def workflow_config(
    companies: Companies,
    instances: AgentInstances,
    company: str,
    name: str,
    *,
    crew_required: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The named workflow's block from the company's applied blueprint,
    and its conductor as `{company, crew_template, conductor,
    conductor_role, conductor_url}`. A workflow that executes through an
    ephemeral crew must bind one; intake dispatches the conductor itself
    and passes `crew_required=False`. Fail-closed: no applied blueprint,
    no enabled workflow, a missing crew, or no converged conductor
    instance each refuse the run before anything is provisioned."""
    spec = companies.get(company) or {}
    config = next((w for w in spec.get("workflows") or [] if w.get("name") == name), None)
    if config is None or not config.get("enabled", False):
        raise RuntimeError(
            f"company {company!r} has no enabled {name} workflow in its applied blueprint"
        )
    if crew_required and not config.get("crew"):
        raise RuntimeError(
            f"{name} workflow binds no crew — execution is ephemeral crews;"
            " the compiler should have refused this blueprint"
        )
    standing = conductor(instances, company, config["conductor"])
    return config, {
        "company": company,
        "crew_template": config.get("crew"),
        "conductor": standing["agent_id"],
        "conductor_role": standing["role"],
        "conductor_url": standing["url"],
    }
