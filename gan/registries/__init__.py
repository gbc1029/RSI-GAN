"""Component registries (opt-in components catalog)."""

from gan.registries.loader import (  # noqa: F401
    COMPONENTS_DIR,
    ComponentRegistry,
    KINDS,
    REGISTRY_DIR,
    load_registry_for_role,
    orphan_modules,
    resolve_registry_file,
    safe_module_rel,
    validate_registry,
)
