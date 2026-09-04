"""The company's documents, as the platform reads them.

The strategy and the architecture masterplan live in the company's
repository at the paths the blueprint names. Every tick and every run
reads them at `base_ref` under the platform identity — never from a
sandbox, whose tree a run could have edited — and hands them on as
untrusted text: they were written by the company, in git, and can say
anything. Absence is a first-class observation, not an error: the first
ticks of a company exist to write them.
"""

from typing import Any

from common.text import clip
from orchestrator.clients import GatewayClient
from registry import Companies

# The two documents, in the order the observation renders them; the
# headers are the contract the mock brain parses.
DOCUMENTS = ("strategy", "architecture")
HEADERS = {"strategy": "Strategy:", "architecture": "Architecture masterplan:"}
ABSENT = "(absent)"


def company_documents(companies: Companies, company: str) -> dict[str, str]:
    """The document paths by name, from the applied blueprint. Fail-closed:
    no applied blueprint, no paths."""
    spec = companies.get(company)
    if not spec:
        raise RuntimeError(f"no applied blueprint for company {company!r}")
    return dict(spec["documents"])


async def read_documents(
    gateway: GatewayClient, run_id: str, repo: dict[str, str], paths: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Each document at `base_ref`: `{path, present, text}` by name, the
    text clipped to what a model may be shown."""
    documents = {}
    for name in DOCUMENTS:
        read = await gateway.read_document(
            run_id, repo["url"], repo["base_ref"], paths[name]
        )
        text = read.get("text") if read.get("present") else None
        documents[name] = {
            "path": paths[name],
            "present": bool(read.get("present")),
            "text": clip(str(text)) if text is not None else None,
        }
    return documents


def present_texts(documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The documents a dispatch carries: name to text, present ones only."""
    return {name: d["text"] for name, d in documents.items() if d.get("present")}


def sections(documents: dict[str, dict[str, Any]]) -> list[str]:
    """The observation's document sections: the header, then the text as
    it stands on `base_ref`, or the absence stated."""
    rendered = []
    for name in DOCUMENTS:
        document = documents.get(name) or {}
        body = document["text"] if document.get("present") else ABSENT
        rendered.append(f"{HEADERS[name]}\n{body}")
    return rendered
