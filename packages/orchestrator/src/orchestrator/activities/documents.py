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

from blueprint.catalog import STEWARDSHIP_CREW
from blueprint.documents import DOCUMENT_NAMES, document_header
from common.tasks import crew_trailer
from common.text import clip_head
from orchestrator.clients import GatewayClient
from registry import Companies

# The observation renders each document under its header; the headers
# are the contract the mock brain parses.
ABSENT = "(absent)"


def bootstrap_body(name: str, path: str, branch: str) -> str:
    """The steward's whole task for writing one absent document: the
    vision is in its prompt, the path is the blueprint's, and the branch
    keeps the delivery off every other run's."""
    what = {
        "strategy": (
            "Write the company's strategy from the vision in your prompt: what "
            "the company builds first, in which order, and why — the document "
            "every later slice is scoped from. It is written once; after it "
            "exists no role may change it."
        ),
        "architecture": (
            "Write the company's architecture masterplan from the vision in "
            "your prompt and from the repository tree in your sandbox: the "
            "shape of the system every crew implements and reviews against. "
            "It changes only through architecture requests."
        ),
    }[name]
    return (
        f"{what}\n\nCreate the file `{path}` with that document and nothing "
        f"else.\n\nDeliver your work on the branch `{branch}`.\n\n"
        f"Filed by the platform: the document is absent on the base ref.\n\n"
        f"{crew_trailer(STEWARDSHIP_CREW)}"
    )


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
    for name in DOCUMENT_NAMES:
        read = await gateway.read_document(
            run_id, repo["url"], repo["base_ref"], paths[name]
        )
        text = read.get("text") if read.get("present") else None
        documents[name] = {
            "path": paths[name],
            "present": bool(read.get("present")),
            "text": clip_head(str(text)) if text is not None else None,
        }
    return documents


def present_texts(documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The documents a dispatch carries: name to text, present ones only."""
    return {name: d["text"] for name, d in documents.items() if d.get("present")}


def sections(documents: dict[str, dict[str, Any]]) -> list[str]:
    """The observation's document sections: the header, then the text as
    it stands on `base_ref`, or the absence stated."""
    rendered = []
    for name in DOCUMENT_NAMES:
        document = documents.get(name) or {}
        body = document["text"] if document.get("present") else ABSENT
        rendered.append(f"{document_header(name)}\n{body}")
    return rendered
