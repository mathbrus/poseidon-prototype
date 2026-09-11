"""The company a worker executes runs for: the one the operator recorded
from the applied Company CR. The worker is not told it — a platform hosts
one company, and the registry says which."""

from registry import Companies


def applied_company(companies: Companies) -> str:
    """The applied company's name. An activity that asks on a cluster with
    no company applied has nothing to run for; raising lets the workflow
    retry into the apply rather than act on a name nobody applied."""
    company = companies.applied()
    if company is None:
        raise RuntimeError("no company is applied")
    return company
