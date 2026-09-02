"""Blueprint schema and catalogs.

The blueprint is the one source of truth the compiler turns into
every enforcement projection. This package owns what a blueprint may say —
JSON Schema for the nine sections plus the versioned role and crew
catalogs — and nothing about what it compiles to.
"""

from schema.catalog import (
    CATALOG_VERSION,
    CREW_TEMPLATES,
    ROLES,
    CrewTemplate,
    Role,
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
    "CrewTemplate",
    "ROLES",
    "Role",
    "ValidationError",
    "load_blueprint",
    "validate_blueprint",
]
