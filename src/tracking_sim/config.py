"""Loading Pydantic config models from YAML.

String values may reference environment variables as ``${NAME}`` or
``${NAME:-default}``, so credentials and hosts stay out of committed files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

__all__ = ["expand_env", "load_yaml", "load_yaml_model"]

M = TypeVar("M", bound=BaseModel)

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Recursively replace ``${NAME}`` and ``${NAME:-default}`` in strings."""
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            found = os.environ.get(name)
            if found:
                return found
            if default is not None:
                return default
            raise KeyError(f"environment variable {name} is not set")

        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_yaml(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as fh:
        return expand_env(yaml.safe_load(fh))


def load_yaml_model(model: type[M], path: str | Path) -> M:
    """Load ``path`` and validate it as ``model``."""
    return model.model_validate(load_yaml(path) or {})
