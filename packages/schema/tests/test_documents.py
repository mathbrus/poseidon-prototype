from schema.documents import DOCUMENT_LABELS, DOCUMENT_NAMES, document_header


def test_every_document_has_a_label_and_a_header():
    """One spelling per document, and every reader derives from it."""
    assert set(DOCUMENT_LABELS) == set(DOCUMENT_NAMES)
    assert document_header("strategy") == "Strategy:"
    assert document_header("architecture") == "Architecture masterplan:"
