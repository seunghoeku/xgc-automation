"""Global sequential simulation IDs and canonical directory names.

``simulation_id`` identifies a scientific simulation across scheduler jobs and
restarts. It is distinct from ``run_id``, which identifies one submission
attempt.

Examples:
    n645au
    n646do
    n647pe_po

The machine name is not repeated in the directory slug:
    n645au_em_kbm_linear
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from tools import input_builder
from tools import machines as machine_config
from tools import simulation_catalog as catalog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = PROJECT_ROOT / "kb" / "simulation_registry.yaml"
_SIMULATION_ID_RE = re.compile(r"^n([0-9]{3,})([a-z]{2}(?:_[a-z]{2})*)$")


class SimulationRegistryError(RuntimeError):
    """The registry or a requested simulation identifier is invalid."""


def _load_registry() -> dict[str, Any]:
    try:
        data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SimulationRegistryError(f"cannot read simulation registry: {exc}") from exc
    if not isinstance(data, dict):
        raise SimulationRegistryError("simulation registry must be a mapping")
    if not isinstance(data.get("next_number"), int):
        raise SimulationRegistryError("simulation registry needs an integer next_number")
    if not isinstance(data.get("machine_codes"), dict):
        raise SimulationRegistryError("simulation registry needs machine_codes")
    if not isinstance(data.get("reservations", []), list):
        raise SimulationRegistryError("simulation registry reservations must be a list")
    return data


def _write_registry(data: dict[str, Any]) -> None:
    """Atomically replace the registry after an approved reservation."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".simulation-registry-",
        suffix=".yaml",
        dir=REGISTRY_PATH.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
        Path(temp_name).replace(REGISTRY_PATH)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _normalize_machine(name: str, registry: dict[str, Any]) -> str:
    normalized = str(name).strip().lower()
    aliases = registry.get("machine_aliases") or {}
    normalized = str(aliases.get(normalized, normalized)).lower()
    if normalized not in registry["machine_codes"]:
        known = sorted(registry["machine_codes"])
        raise SimulationRegistryError(
            f"unknown machine {name!r}; known machines: {known}"
        )
    return normalized


def _normalize_machines(
    machines: str | Iterable[str],
    registry: dict[str, Any],
) -> list[str]:
    raw = [machines] if isinstance(machines, str) else list(machines)
    if not raw:
        raise SimulationRegistryError("at least one machine is required")
    normalized: list[str] = []
    for item in raw:
        name = _normalize_machine(item, registry)
        if name in normalized:
            raise SimulationRegistryError(f"machine {name!r} was specified more than once")
        normalized.append(name)
    return normalized


def _ascii_words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return [part.lower() for part in re.findall(r"[A-Za-z0-9]+", text)]


def _slugify(
    short_name: str,
    machines: list[str],
    registry: dict[str, Any],
) -> str:
    """Make an ASCII physics-purpose slug and remove machine-only tokens."""
    excluded = set(machines)
    excluded.update(registry["machine_codes"][name] for name in machines)
    aliases = registry.get("machine_aliases") or {}
    excluded.update(
        alias for alias, target in aliases.items() if str(target).lower() in machines
    )
    words = [word for word in _ascii_words(short_name) if word not in excluded]
    slug = "_".join(words)
    if short_name.strip() and not slug:
        raise SimulationRegistryError(
            "short_name contains only machine names/codes or unsupported characters; "
            "include a physics or purpose label"
        )
    return slug


def _normalize_tags(
    tags: Iterable[str] | None,
    machines: list[str],
    registry: dict[str, Any],
) -> list[str]:
    machine_tokens = set(machines)
    machine_tokens.update(registry["machine_codes"][name] for name in machines)
    machine_tokens.update(
        alias
        for alias, target in (registry.get("machine_aliases") or {}).items()
        if str(target).lower() in machines
    )
    normalized: list[str] = []
    for tag in tags or []:
        value = "-".join(_ascii_words(str(tag)))
        if value and value not in machine_tokens and value not in normalized:
            normalized.append(value)
    return normalized


def _format_id(number: int, codes: list[str]) -> str:
    if number < 0:
        raise SimulationRegistryError("simulation number must be non-negative")
    return f"n{number:03d}{'_'.join(codes)}"


def plan_simulation_id(
    machines: str | Iterable[str],
    short_name: str = "",
    *,
    tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return the next ID and directory name without changing the registry."""
    registry = _load_registry()
    normalized_machines = _normalize_machines(machines, registry)
    codes = [registry["machine_codes"][name] for name in normalized_machines]
    number = registry["next_number"]
    simulation_id = _format_id(number, codes)
    slug = _slugify(short_name, normalized_machines, registry)
    directory_name = simulation_id if not slug else f"{simulation_id}_{slug}"
    return {
        "simulation_id": simulation_id,
        "simulation_number": number,
        "machines": normalized_machines,
        "machine_codes": codes,
        "short_name": slug,
        "directory_name": directory_name,
        "tags": _normalize_tags(tags, normalized_machines, registry),
        "registry_next_number": number,
        "writes": False,
    }


def reserve_simulation_id(
    machines: str | Iterable[str],
    short_name: str = "",
    *,
    tags: Iterable[str] | None = None,
    expected_number: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Reserve the next global number after explicit human confirmation."""
    plan = plan_simulation_id(machines, short_name, tags=tags)
    if not confirm:
        return {
            "reserved": False,
            "reason": (
                "confirmation required; review the simulation ID and directory "
                "name, then resubmit with confirm=True and expected_number"
            ),
            "plan": plan,
        }
    if expected_number is None:
        raise SimulationRegistryError(
            "expected_number is required when confirm=True; use the number shown "
            "by plan_simulation_id"
        )
    if expected_number != plan["simulation_number"]:
        raise SimulationRegistryError(
            f"stale simulation ID plan: expected {expected_number}, but the next "
            f"number is now {plan['simulation_number']}"
        )

    registry = _load_registry()
    existing_numbers = {
        int(item["simulation_number"])
        for item in registry.get("reservations", [])
        if isinstance(item, dict) and isinstance(item.get("simulation_number"), int)
    }
    historical_through = int(registry.get("historical_through", -1))
    if expected_number <= historical_through or expected_number in existing_numbers:
        raise SimulationRegistryError(
            f"simulation number {expected_number} has already been used"
        )

    record = {
        key: plan[key]
        for key in (
            "simulation_id",
            "simulation_number",
            "machines",
            "machine_codes",
            "short_name",
            "directory_name",
            "tags",
        )
    }
    record["status"] = "reserved"
    record["reserved_at"] = datetime.now(timezone.utc).isoformat()
    registry.setdefault("reservations", []).append(record)
    registry["next_number"] = expected_number + 1
    _write_registry(registry)

    # Open the simulation's catalog record at the moment the number exists, so
    # the number is never a bare integer waiting for someone to remember to
    # write it down. Everything later -- runs, status, archive location -- is
    # appended to this file.
    catalog_record = catalog.create_record(
        record["simulation_id"],
        machines=record["machines"],
        short_name=record["short_name"],
        directory_name=record["directory_name"],
        tags=record["tags"],
        status="reserved",
        origin="automation",
    )
    catalog_path = catalog.record_path(record["simulation_id"])
    try:
        catalog_display = str(catalog_path.relative_to(PROJECT_ROOT))
    except ValueError:
        catalog_display = str(catalog_path)
    return {
        "reserved": True,
        **record,
        "next_number": registry["next_number"],
        "catalog_record": catalog_display,
        "catalog_status": catalog_record["status"],
    }


def get_simulation(simulation_id: str) -> dict[str, Any]:
    """Return one reserved simulation record."""
    if not _SIMULATION_ID_RE.fullmatch(simulation_id):
        raise SimulationRegistryError(f"invalid simulation_id {simulation_id!r}")
    registry = _load_registry()
    for record in registry.get("reservations", []):
        if isinstance(record, dict) and record.get("simulation_id") == simulation_id:
            return dict(record)
    raise SimulationRegistryError(f"simulation_id {simulation_id!r} is not reserved")


def simulation_directory(simulation_id: str, machine: str | None = None) -> dict[str, Any]:
    """Resolve the canonical remote directory for a reserved simulation."""
    record = get_simulation(simulation_id)
    machines = list(record["machines"])
    if machine is None:
        if len(machines) != 1:
            raise SimulationRegistryError(
                f"{simulation_id} uses multiple machines {machines}; specify machine"
            )
        machine = machines[0]
    registry = _load_registry()
    machine = _normalize_machine(machine, registry)
    if machine not in machines:
        raise SimulationRegistryError(
            f"{simulation_id} is reserved for {machines}, not {machine!r}"
        )

    try:
        cfg = machine_config.get_machine(machine)
    except KeyError as exc:
        raise SimulationRegistryError(
            f"machine {machine!r} has an ID code but no machines.yaml entry yet"
        ) from exc
    run_root = cfg.get("paths", {}).get("run_root")
    if not run_root or machine_config.is_placeholder(run_root):
        raise SimulationRegistryError(
            f"paths.run_root for {machine!r} is not configured"
        )
    remote_dir = f"{str(run_root).rstrip('/')}/{record['directory_name']}"
    return {
        "simulation_id": simulation_id,
        "machine": machine,
        "directory_name": record["directory_name"],
        "remote_directory": remote_dir,
        "tags": list(record.get("tags") or []),
    }


def stage_simulation_case(
    case: str,
    simulation_id: str,
    *,
    machine: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Stage a baseline into its canonical simulation directory."""
    directory = simulation_directory(simulation_id, machine)
    result = input_builder.stage_case(
        case,
        directory["remote_directory"],
        confirm=confirm,
        machine=directory["machine"],
    )
    result["simulation_id"] = simulation_id
    result["directory_name"] = directory["directory_name"]
    result["tags"] = directory["tags"]

    # Staging is the first thing that puts bytes on the machine, so it is the
    # point at which the catalog can honestly say where the simulation lives.
    if confirm and result.get("staged"):
        try:
            record = catalog.read_record(simulation_id)
        except catalog.SimulationCatalogError:
            record = None
        if record is not None:
            remote_dirs = dict(record.get("remote_dirs") or {})
            remote_dirs[directory["machine"]] = directory["remote_directory"]
            updates: dict[str, Any] = {"remote_dirs": remote_dirs, "case": case}
            if record.get("status") == "reserved":
                updates["status"] = "staged"
            catalog.update_record(simulation_id, updates)
            result["catalog_status"] = updates.get("status", record.get("status"))
    return result
