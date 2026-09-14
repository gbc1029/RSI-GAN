"""Config layer for the GAN framework."""

from gan.config.loader import (  # noqa: F401
    Config,
    config_dir,
    load_eval_points,
    load_gan_loop_config,
    load_prompt,
    load_registry,
    resolve_domain,
    validate,
)
