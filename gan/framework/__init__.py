"""Framework layer (frozen): hyperparameters + domain registry + loaders."""

from gan.framework.loader import (  # noqa: F401
    Config,
    config_dir,
    list_registered_domains,
    load_gan_loop_config,
    load_registry,
    resolve_domain,
    validate,
)
