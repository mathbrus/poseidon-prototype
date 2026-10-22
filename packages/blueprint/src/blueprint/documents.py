"""The company's two documents, named once.

A blueprint places both (`company.documents`), the platform reads them
at the base ref before every tick and every run, the steward writes
them, and every reader shows them under the same label — the
observation's section headers and the crews' untrusted context alike.
"""

from common.documents import DOCUMENT_NAMES

# The label a model sees a document under.
DOCUMENT_LABELS = {"strategy": "strategy", "architecture": "architecture masterplan"}


def document_header(name: str) -> str:
    """The observation's section header for the document."""
    return DOCUMENT_LABELS[name].capitalize() + ":"


__all__ = ["DOCUMENT_LABELS", "DOCUMENT_NAMES", "document_header"]
