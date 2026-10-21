"""Reading a workflow's shape, and its conductor, out of the applied
blueprint and the converged standing instances."""

from typing import Any

from blueprint.bounds import render_bounds
from common.topology import agent_url
from registry import AgentInstances, Companies


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
        "url": agent_url(standing_name, instance["namespace"]),
    }


def company_repo(companies: Companies, company: str) -> dict[str, str]:
    """The repository the company works on, `{url, base_ref}`, from its
    applied blueprint. Fail-closed: no applied blueprint, no repository."""
    spec = companies.get(company)
    if not spec:
        raise RuntimeError(f"no applied blueprint for company {company!r}")
    return dict(spec["repo"])


def workflow_config(
    companies: Companies,
    instances: AgentInstances,
    company: str,
    name: str,
    crewless: bool = False,
) -> dict[str, Any]:
    """The named workflow's shape, from the company's applied blueprint:

        {company, conductor: {agent_id, role, url}, crew, gates,
         max_verify_retries, budget_deadline_minutes, contract_paths,
         bounds}

    The conductor is the company's one, named on the heartbeat section.
    `crew` is the ephemeral crew the workflow executes through, or None
    for a `crewless` workflow — one that dispatches the conductor directly
    and declares it has no node.
    `gates` is the block as the blueprint wrote it (`{merge: {resolved_by,
    deadline_minutes, delegate?}}`, ...), `max_verify_retries` bounds
    implement's verify loop, `budget_deadline_minutes` is how long a run
    stopped at the budget cap waits for a human, and `contract_paths` is
    the declared contract tree ([] when the company declares none) that
    review's deletion facts resolve against, and `bounds` is the charter as
    it binds the company's work, rendered once here and carried by every
    dispatch of the run into the procedure's system prompt.
    Fail-closed: no applied blueprint, no node for a crewed workflow, or
    no converged conductor instance each refuse the run before anything
    is provisioned."""
    spec = companies.get(company)
    if not spec:
        raise RuntimeError(f"no applied blueprint for company {company!r}")
    config = next((w for w in spec.get("workflows") or [] if w.get("name") == name), None)
    if config is None and not crewless:
        raise RuntimeError(
            f"company {company!r} declares no {name} workflow in its applied blueprint"
        )
    config = config or {}
    return {
        "company": company,
        "conductor": conductor(instances, company, spec["heartbeat"]["conductor"]),
        "crew": config.get("crew"),
        "gates": dict(config.get("gates") or {}),
        "max_verify_retries": config.get("max_verify_retries"),
        "budget_deadline_minutes": int(spec["budgets"]["deadline_minutes"]),
        "contract_paths": sorted(
            (spec["policies"].get("contract_tests") or {}).get("paths") or []
        ),
        "bounds": render_bounds(spec),
    }
