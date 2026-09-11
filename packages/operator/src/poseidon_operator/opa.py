"""The one read the operator makes of OPA: which revision of a bundle it
has activated.

Bundles reach OPA by OPA's own polling of the bundle server; nothing here
pushes. OPA records every bundle it activates under
`data.system.bundles.<name>.manifest` — the `.manifest` the registry
packed, revision included — so comparing what it reports with what the
operator stored says whether the stored authority is the served one.
"""

import httpx


def activated_revision(opa_url: str, name: str) -> str | None:
    """The revision OPA serves for bundle `name`, or None when it has
    activated nothing under that name. An unreachable or failing OPA is
    also None: to the caller both mean "not live yet", something to wait
    for rather than an exception to crash a reconcile on."""
    url = f"{opa_url.rstrip('/')}/v1/data/system/bundles/{name}/manifest/revision"
    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    revision = response.json().get("result")
    return revision if isinstance(revision, str) else None
