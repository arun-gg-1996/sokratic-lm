"""
config.py
---------
Loads config/base.yaml deep-merged with config/domains/{domain}.yaml.
Active domain is selected via the SOKRATIC_DOMAIN environment variable (default: "ot").

Every module imports `cfg` from here — nothing reads yaml files directly.
"""

import os
import yaml
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_DIR = ROOT / "config"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on conflict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load() -> dict:
    with open(CONFIG_DIR / "base.yaml", "r") as f:
        base = yaml.safe_load(f) or {}

    domain_id = os.environ.get("SOKRATIC_DOMAIN", "ot").lower().strip()
    domain_file = CONFIG_DIR / "domains" / f"{domain_id}.yaml"
    if not domain_file.exists():
        raise FileNotFoundError(
            f"Domain config not found: {domain_file}. "
            f"Set SOKRATIC_DOMAIN to a valid domain (e.g. 'ot', 'physics')."
        )
    with open(domain_file, "r") as f:
        domain_cfg = yaml.safe_load(f) or {}

    return _deep_merge(base, domain_cfg)


_raw = _load()


class _Section:
    """Wraps a config dict section so values are accessible as attributes."""
    def __init__(self, data: dict):
        for k, v in data.items():
            setattr(self, k, _Section(v) if isinstance(v, dict) else v)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __repr__(self):
        return str(self.__dict__)


class Config:
    models     = _Section(_raw["models"])
    retrieval  = _Section(_raw["retrieval"])
    session    = _Section(_raw["session"])
    dean       = _Section(_raw["dean"])
    thresholds = _Section(_raw["thresholds"])
    memory     = _Section(_raw["memory"])
    domain     = _Section(_raw["domain"])
    simulation = _Section(_raw["simulation"])
    paths      = _Section(_raw["paths"])
    qdrant     = _Section(_raw["qdrant"])
    prompts    = _Section(_raw["prompts"])
    query_aliases = _raw.get("query_aliases", {}) or {}
    topic_index   = _Section(_raw.get("topic_index", {}) or {})

    @staticmethod
    def reload():
        """Re-read config files at runtime (useful during development)."""
        global _raw
        _raw = _load()
        Config.models     = _Section(_raw["models"])
        Config.retrieval  = _Section(_raw["retrieval"])
        Config.session    = _Section(_raw["session"])
        Config.dean       = _Section(_raw["dean"])
        Config.thresholds = _Section(_raw["thresholds"])
        Config.memory     = _Section(_raw["memory"])
        Config.domain     = _Section(_raw["domain"])
        Config.simulation = _Section(_raw["simulation"])
        Config.paths      = _Section(_raw["paths"])
        Config.qdrant     = _Section(_raw["qdrant"])
        Config.prompts    = _Section(_raw["prompts"])
        Config.query_aliases = _raw.get("query_aliases", {}) or {}
        Config.topic_index   = _Section(_raw.get("topic_index", {}) or {})


cfg = Config()
