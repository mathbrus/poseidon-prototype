"""The OPA naming contract shared by the compiler, the gateways and the
orchestrator: where a company's compiled policy lives, and how a name
becomes a Rego identifier.

The compiler emits a company's bundle under `company_package`; the gateway
and the orchestrator query its decisions at `company_decision_path`. One
transform, so a company can never end up with its callers querying an
empty document — which would fail closed (every call denied) but silently.
"""

PLATFORM_DECISION_PATH = "poseidon/platform/decision"


def rego_slug(value: str) -> str:
    """A name as a Rego identifier segment: dots, hyphens and slashes become
    underscores."""
    return value.replace(".", "_").replace("-", "_").replace("/", "_")


def company_package(company: str) -> str:
    """The company bundle's Rego package — also its OPA bundle root."""
    return f"poseidon.authz.{rego_slug(company)}"


def company_decision_path(company: str) -> str:
    """The OPA data path of the company's `decision` document."""
    return f"{company_package(company).replace('.', '/')}/decision"
