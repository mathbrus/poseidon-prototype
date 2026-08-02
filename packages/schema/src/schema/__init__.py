"""Blueprint schema and catalogs (plan step 7).

The blueprint is the one source of truth the compiler (step 9) turns into
every enforcement projection. This package owns what a blueprint may say —
JSON Schema for the eight sections plus the versioned role and crew
template catalogs — and nothing about what it compiles to.
"""

from schema.catalog import (
    CATALOG_VERSION,
    CREW_TEMPLATES,
    ROLE_TEMPLATES,
    CrewMember,
    CrewTemplate,
    RoleTemplate,
)
from schema.validate import (
    BLUEPRINT_SCHEMA,
    ValidationError,
    load_blueprint,
    validate_blueprint,
)

__all__ = [
    "BLUEPRINT_SCHEMA",
    "CATALOG_VERSION",
    "CREW_TEMPLATES",
    "CrewMember",
    "CrewTemplate",
    "ROLE_TEMPLATES",
    "RoleTemplate",
    "ValidationError",
    "load_blueprint",
    "validate_blueprint",
]
