"""Reading a workflow's shape, and its conductor, out of the applied
blueprint and the converged standing instances."""

from typing import Any

from registry import AgentInstances, Companies

AGENT_PORT = 8000


def conductor(
    instances: AgentInstances, company: str, standing_name: str
) -> dict[str, Any]:
    """The converged standing instance behind a blueprint's conductor name,
    as `{agent_id, role, url}`: the identity a crew request is recorded
    under and the address a dispatch reaches it at. The operator names the
    instance's Service after the hire in the company namespace, so the
    address is derived, never configured. Fail-closed: no converged
    instance refuses the run before anything is provisioned."""
    instance = next(
        (
            i
            for i in instances.for_company(company)
            if i.get("standing_name") == standing_name
        ),
        None,
    )
    if instance is None:
        raise RuntimeError(
            f"no standing instance for conductor {standing_name!r}"
            " — is the company converged?"
        )
    return {
        "agent_id": instance["agent_id"],
        "role": instance["role"],
        "url": f"http://{standing_name}.{instance['namespace']}.svc.cluster.local:{AGENT_PORT}",
    }


def workflow_config(
    companies: Companies, instances: AgentInstances, company: str, name: str
) -> dict[str, Any]:
    """The named workflow's shape, from the company's applied blueprint:

        {company, conductor: {agent_id, role, url}, crew, gates,
         max_verify_retries, contract_paths}

    `crew` is the ephemeral crew the workflow executes through, or None
    for a workflow that dispatches its conductor directly (intake).
    `gates` is the block as the blueprint wrote it (`{merge: {resolved_by,
    delegate?}}`, ...), `max_verify_retries` bounds implement's verify
    loop, and `contract_paths` is the declared contract tree ([] when the
    company declares none) that review's deletion facts resolve against.
    Fail-closed: no applied blueprint, no enabled workflow, or no
    converged conductor instance each refuse the run before anything is
    provisioned."""
    spec = companies.get(company) or {}
    config = next((w for w in spec.get("workflows") or [] if w.get("name") == name), None)
    if config is None or not config.get("enabled", False):
        raise RuntimeError(
            f"company {company!r} has no enabled {name} workflow in its applied blueprint"
        )
    return {
        "company": company,
        "conductor": conductor(instances, company, config["conductor"]),
        "crew": config.get("crew"),
        "gates": dict(config.get("gates") or {}),
        "max_verify_retries": int(config.get("max_verify_retries") or 0),
        "contract_paths": sorted((spec.get("contractTests") or {}).get("paths") or []),
    }
