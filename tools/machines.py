"""Loader for machines.yaml plus the per-user machines.local.yaml overlay.

Machine definitions come from two layers:

1. ``machines.yaml`` (committed) -- facts about the machines themselves:
   hosts, schedulers, queue shapes, shared installs. True for every user.
2. ``machines.local.yaml`` (gitignored) -- one user's bindings: username,
   account, scratch paths, personal XGC builds, ssh alias names. Copy
   ``machines.local.yaml.example`` and edit; without it, user-specific
   fields stay as ``<placeholders>`` and preflight reports what is missing.

The overlay is a deep merge: dicts merge recursively, scalars and lists in
the local file replace the committed value. Higher-level logic must remain
unaware of the machine type; this layer absorbs scheduler, authentication,
and path differences (see AGENTS.md).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Project root = parent of the parent of this file (tools/machines.py)
from tools import paths

PROJECT_ROOT = paths.PROJECT_ROOT
MACHINES_FILE = paths.MACHINES_FILE
# The personal overlay: the checkout's copy, else the one next to the state
# (XGC_CATALOG_DIR); see tools/paths.py.
LOCAL_MACHINES_FILE = paths.LOCAL_MACHINES_FILE
_definitions: ContextVar[dict[str, Any] | None] = ContextVar("machines", default=None)


@contextmanager
def using_machines(definitions: dict[str, Any]):
    """Use explicit definitions without loading repository configuration."""
    token = _definitions.set(definitions)
    try:
        yield
    finally:
        _definitions.reset(token)


def _active_machines() -> dict[str, Any]:
    configured = _definitions.get()
    return load_machines() if configured is None else configured


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into base without mutating either.

    Dicts merge key by key; any other type (scalar, list) in the overlay
    replaces the base value outright, so a user can override a whole list
    (e.g. modules) without surprise concatenation.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def load_machines() -> dict[str, Any]:
    """machines.yaml merged with the machines.local.yaml overlay, if present."""
    if not MACHINES_FILE.exists():
        raise FileNotFoundError(f"machines.yaml not found: {MACHINES_FILE}")
    with MACHINES_FILE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    if LOCAL_MACHINES_FILE.exists():
        with LOCAL_MACHINES_FILE.open("r", encoding="utf-8") as fh:
            local = yaml.safe_load(fh) or {}
        unknown = set(local) - set(data)
        if unknown:
            raise ValueError(
                f"machines.local.yaml defines machines not present in "
                f"machines.yaml: {sorted(unknown)}. Add the machine to "
                f"machines.yaml first (its universal facts), then bind your "
                f"account in the local overlay."
            )
        data = _deep_merge(data, local)

    return data


def machine_names() -> list[str]:
    """Names of the actual machines, excluding the `common` section."""
    return [k for k in _active_machines().keys() if k != "common"]


def get_machine(name: str) -> dict[str, Any]:
    """Return a machine definition by name. Raises KeyError if unknown."""
    data = _active_machines()
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
