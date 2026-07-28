"""Loader for machines.yaml.

Machine definitions are read only from machines.yaml, the single source of truth.
Higher-level logic must remain unaware of the machine type; this layer absorbs
scheduler, authentication, and path differences (see AGENTS.md).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Project root = parent of the parent of this file (tools/machines.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACHINES_FILE = PROJECT_ROOT / "machines.yaml"


@lru_cache(maxsize=1)
def load_machines() -> dict[str, Any]:
    """Return the entire contents of machines.yaml as a dict."""
    if not MACHINES_FILE.exists():
        raise FileNotFoundError(f"machines.yaml not found: {MACHINES_FILE}")
    with MACHINES_FILE.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def machine_names() -> list[str]:
    """Names of the actual machines, excluding the `common` section."""
    return [k for k in load_machines().keys() if k != "common"]


def get_machine(name: str) -> dict[str, Any]:
    """Return a machine definition by name. Raises KeyError if unknown."""
    data = load_machines()
    if name not in data or name == "common":
        raise KeyError(f"unknown machine: {name!r}. available: {machine_names()}")
    return data[name]


def is_placeholder(value: Any) -> bool:
    """Whether the value is an unfilled `<...>` placeholder."""
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def expand_path(value: str) -> str:
    """Expand `~` and environment variables locally.

    Remote paths may deliberately be left unexpanded.
    """
    return os.path.expandvars(os.path.expanduser(value))
