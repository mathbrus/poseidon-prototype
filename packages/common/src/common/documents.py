"""The company's two documents, by name — what a blueprint places
(`company.documents`), what the platform reads and the steward writes,
and what the console interface names — in the order every reader renders
them."""

DOCUMENT_NAMES = ("strategy", "architecture")

# The fixed titles of the tickets the platform files for an absent
# document: what the observation matches an open ticket against, so a
# document is never bootstrapped twice, and what the gateway refuses to
# let the PM dismiss.
BOOTSTRAP_TITLES = {
    "strategy": "Bootstrap: strategy",
    "architecture": "Bootstrap: architecture masterplan",
}
