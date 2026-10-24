"""Blueprint schema and catalogs.

The blueprint is the one source of truth the compiler turns into
every enforcement projection. This package owns what a blueprint may say —
JSON Schema for the nine sections plus the versioned role and crew
catalogs — and how the applied blueprint is said to the agents it binds
(`bounds`); nothing about what it compiles to.
"""

from blueprint.bounds import render_bounds
from blueprint.catalog import (
    CREW_TEMPLATES,
    ROLES,
    CrewTemplate,
    Role,
)
from blueprint.validate import (
    BLUEPRINT_SCHEMA,
    ValidationError,
    load_blueprint,
    validate_blueprint,
)

__all__ = [
    "BLUEPRINT_SCHEMA",
    "CREW_TEMPLATES",
    "CrewTemplate",
    "ROLES",
    "Role",
    "ValidationError",
    "load_blueprint",
    "render_bounds",
    "validate_blueprint",
]
