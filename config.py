"""
config.py
---------
Loads config.yaml and exposes all settings as a single Config object.
Every module imports from here — nothing reads config.yaml directly.
"""

import yaml
from pathlib import Path

ROOT = Path(__file__).parent


def _load() -> dict:
    with open(ROOT / "config.yaml", "r") as f:
        return yaml.safe_load(f)


_raw = _load()


class _Section:
    """Wraps a config dict section so values are accessible as attributes."""
    def __init__(self, data: dict):
        for k, v in data.items():
            setattr(self, k, v)

    def __repr__(self):
        return str(self.__dict__)


class Config:
    models     = _Section(_raw["models"])
    retrieval  = _Section(_raw["retrieval"])
    session    = _Section(_raw["session"])
    dean       = _Section(_raw["dean"])
    thresholds = _Section(_raw["thresholds"])
    memory     = _Section(_raw["memory"])
    simulation = _Section(_raw["simulation"])
    paths      = _Section(_raw["paths"])
    qdrant     = _Section(_raw["qdrant"])
    prompts    = _Section(_raw["prompts"])

    @staticmethod
    def reload():
        """Re-read config.yaml at runtime (useful during development)."""
        global _raw
        _raw = _load()
        Config.models = _Section(_raw["models"])
        Config.retrieval = _Section(_raw["retrieval"])
        Config.session = _Section(_raw["session"])
        Config.dean = _Section(_raw["dean"])
        Config.thresholds = _Section(_raw["thresholds"])
        Config.memory = _Section(_raw["memory"])
        Config.simulation = _Section(_raw["simulation"])
        Config.paths = _Section(_raw["paths"])
        Config.qdrant = _Section(_raw["qdrant"])
        Config.prompts = _Section(_raw["prompts"])


cfg = Config()
