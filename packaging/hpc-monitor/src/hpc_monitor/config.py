"""A standalone server list: no simulation inputs or repository configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def app_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HPC Queue Monitor"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hpc-monitor"


def default_config() -> Path:
    return app_dir() / "servers.yaml"


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """A repeated server name must not silently discard a server definition."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigError("YAML keys must be strings")
        if key in result:
            raise ConfigError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _keys(data: Any, allowed: set[str], location: str) -> dict:
    if not isinstance(data, dict):
        raise ConfigError(f"{location} must be a mapping")
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown fields in {location}: {', '.join(sorted(unknown))}")
    return data


def _integer(data: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    path: Path
    machines: dict[str, dict]
    poll_interval: int = 600
    history_limit: int = 5
    notify: bool = True

    @property
    def cache_dir(self) -> Path:
        identity = hashlib.sha256(str(self.path).encode()).hexdigest()[:16]
        return app_dir() / "cache" / identity

    def state_files(self) -> dict[str, Path]:
        # Changing the account/host behind a name must start a new watch history.
        return {
            name: self.cache_dir / (
                name + "-" + hashlib.sha256(json.dumps(cfg, sort_keys=True).encode())
                .hexdigest()[:16] + ".json"
            )
            for name, cfg in self.machines.items()
        }


def load(path: Path) -> Config:
    path = path.expanduser().resolve()
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}. Run 'hpc-monitor init' to begin.") from exc
    data = _keys(data, {"version", "poll_interval", "history_limit", "notify", "servers"}, "config")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ConfigError("version must be 1")
    interval = _integer(data, "poll_interval", 600, 60, 86400)
    history = _integer(data, "history_limit", 5, 1, 100)
    notify = data.get("notify", True)
    if type(notify) is not bool:
        raise ConfigError("notify must be true or false")
    servers = data.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise ConfigError("servers must contain at least one server")
    machines = {}
    for name, server in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) or name == "common":
            raise ConfigError(f"Invalid server name: {name!r}; use letters, digits, - or _")
        server = _keys(server, {"host", "ssh_host", "user", "scheduler", "auth", "enabled"}, name)
        if type(server.get("enabled", True)) is not bool:
            raise ConfigError(f"{name}.enabled must be true or false")
        if not server.get("enabled", True):
            continue
        for key in ("host", "ssh_host", "user"):
            value = server.get(key)
            if key in {"host", "ssh_host"} and value is None:
                continue
            if (not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@+-]*", value)):
                raise ConfigError(f"Set {name}.{key} to a valid SSH alias or username")
        if not server.get("host") and not server.get("ssh_host"):
            raise ConfigError(f"{name} needs host or ssh_host")
        scheduler = server.get("scheduler")
        if scheduler not in ("slurm", "pbs"):
            raise ConfigError(f"{name}.scheduler must be slurm or pbs")
        auth = server.get("auth", "controlmaster")
        if auth not in ("controlmaster", "key", "sshproxy"):
            raise ConfigError(f"{name}.auth must be controlmaster, key, or sshproxy")
        machines[name] = {
            "host": server.get("host"),
            "user": server["user"], "scheduler": {"type": scheduler},
            "auth": {"method": auth, "ssh_config_host": server.get("ssh_host")},
        }
    if not machines:
        raise ConfigError("Enable at least one server")
    return Config(path, machines, interval, history, notify)
