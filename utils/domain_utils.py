"""Domain registry accessors.

Backed by ``gan/config/registry.yaml`` when importable; otherwise falls back to
the legacy hard-coded values so behaviour is preserved. Function signatures are
unchanged for backward compatibility.
"""
from __future__ import annotations

_LEGACY = {
    "paper_review": dict(score_key="overall_accuracy", splits=["train", "val"], ensembled=True,
                         eval_subset="_filtered_100_train", test_subset="_filtered_100_test",
                         stagedeval_samples=10, stagedeval_frac=10 / 100, has_val_subset=True),
    "search_arena": dict(score_key="overall_accuracy", splits=["train", "val"], ensembled=True,
                         eval_subset="_filtered_100_train", test_subset="_filtered_100_test",
                         stagedeval_samples=10, stagedeval_frac=10 / 100, has_val_subset=True),
    "imo_grading": dict(score_key="overall_accuracy", splits=["train", "val"], ensembled=True,
                        eval_subset="_filtered_100_train", test_subset="_filtered_100_test",
                        stagedeval_samples=10, stagedeval_frac=10 / 100, has_val_subset=True),
    "polyglot": dict(score_key="accuracy_score", splits=["train"], ensembled=False,
                     eval_subset="", test_subset="", stagedeval_samples=10,
                     stagedeval_frac=10 / 60, has_val_subset=False),
    "imo_proof": dict(score_key="points_percentage", splits=["train"], ensembled=False,
                      eval_subset="", test_subset="", stagedeval_samples=10,
                      stagedeval_frac=10 / 60, has_val_subset=False),
    "balrog_babyai": dict(score_key="average_progress", splits=["train"], ensembled=False,
                          eval_subset="", test_subset="", stagedeval_samples=1,
                          stagedeval_frac=1 / 10, has_val_subset=False),
    "balrog_minihack": dict(score_key="average_progress", splits=["train"], ensembled=False,
                            eval_subset="", test_subset="", stagedeval_samples=1,
                            stagedeval_frac=1 / 5, has_val_subset=False),
    "genesis": dict(score_key="average_fitness", splits=["train"], ensembled=False,
                    eval_subset="", test_subset="", stagedeval_samples=3,
                    stagedeval_frac=3 / 6, has_val_subset=False),
}


def _legacy_key(domain):
    if domain in ["search_arena", "paper_review", "imo_grading"]:
        return "search_arena" if False else domain
    if "balrog_babyai" in domain:
        return "balrog_babyai"
    if "balrog_minihack" in domain:
        return "balrog_minihack"
    if "balrog" in domain:
        return "balrog_babyai"
    if "genesis" in domain:
        return "genesis"
    if "polyglot" in domain:
        return "polyglot"
    if domain == "imo_proof":
        return "imo_proof"
    return domain


_REGISTRY_LOAD_WARNED = False


def _warn_registry_unavailable(exc: Exception) -> None:
    """B8: warn ONCE per process (stderr) when the gan registry is PRESENT but
    broken -- every ``_field`` lookup would then silently serve legacy defaults
    (old dataset paths / score keys) with zero signal. A standalone DGMH
    deployment without the gan layer is the *intended* mode and stays silent."""
    global _REGISTRY_LOAD_WARNED
    if _REGISTRY_LOAD_WARNED:
        return
    _REGISTRY_LOAD_WARNED = True
    print(f"[WARN] gan domain registry PRESENT but unusable "
          f"({type(exc).__name__}: {exc}); every domain lookup silently falls back "
          f"to legacy defaults — check gan/framework/loader (domains.yaml) integrity")


def _load_registry():
    # B8: fail-open with a LOUD first occurrence, split by cause --
    #   ImportError  -> gan layer absent: standalone DGMH run; legacy fallback is
    #                   the intended mode (silent, by design);
    #   anything else-> registry exists but is broken (unparseable yaml, schema
    #                   drift): warn once, then legacy fallback.
    try:
        from gan.framework.loader import load_registry, resolve_domain  # type: ignore
    except ImportError:
        return None, None
    except Exception as e:
        _warn_registry_unavailable(e)
        return None, None
    try:
        return load_registry(), resolve_domain
    except Exception as e:
        _warn_registry_unavailable(e)
        return None, None


_REGISTRY, _RESOLVE = _load_registry()


def _field(domain, name, default=None):
    """Resolve a field for a domain: registry first, legacy fallback."""
    if _REGISTRY is not None and _RESOLVE is not None:
        try:
            cfg = _RESOLVE(_REGISTRY, domain)
            val = cfg.get(name)
            if val is not None:
                return val
        except Exception:
            pass
    legacy = _LEGACY.get(_legacy_key(domain), {})
    return legacy.get(name, default)


def get_domain_score_key(domain):
    return _field(domain, "score_key", None)


def get_domain_splits(domain, eval_test=False):
    splits = list(_field(domain, "splits", ["train"]))
    if eval_test and "test" not in splits:
        splits.append("test")
    return splits


def can_domain_ensembled(domain):
    return bool(_field(domain, "ensembled", False))


def get_domain_eval_subset(domain):
    return _field(domain, "eval_subset", "")


def get_domain_test_subset(domain):
    return _field(domain, "test_subset", "")


def get_domain_stagedeval_samples(domain):
    return _field(domain, "stagedeval_samples", 10)


def get_domain_stagedeval_frac(domain):
    return _field(domain, "stagedeval_frac", 0.1)


def has_domain_val_subset(domain):
    return bool(_field(domain, "has_val_subset", False))
