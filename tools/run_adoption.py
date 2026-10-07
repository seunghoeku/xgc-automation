"""Adopt existing provenance records into the simulation catalog.

The catalog links a simulation to its runs from the moment its number is
reserved. Runs submitted before that existed have provenance records under
``runs/`` but no catalog entry, so they are invisible to ``xgc-sim search`` --
which is exactly the work most worth finding, since it is recent and the
directories are still on disk.

This module closes that gap. It groups orphaned provenance records into
simulations and writes one catalog record per group, marked ``origin:
adopted``.

Two decisions shape it, both deliberate:

* **Runs group by working directory.** A restart reuses the directory and takes
  a new ``run_id``, so the directory is what identifies the scientific
  simulation. Grouping by anything else would either split a campaign across
  records or merge unrelated work.
* **No sequence numbers are assigned.** Reserving numbers retroactively is
  irreversible, and the sequence in ``kb/simulation_registry.yaml`` is reserved
  for simulations a human chose to number. An adopted record is keyed by its
  directory name, has ``number: null``, and says so. A human who decides a run
  deserves a number can reserve one and link it.

Adoption reads provenance and writes catalog records. It never touches the
remote machine and never modifies the provenance it read.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from tools import provenance as prov
from tools import simulation_catalog as catalog

from tools import paths

PROJECT_ROOT = paths.PROJECT_ROOT
RUNS_DIR = paths.RUNS_DIR  # follows XGC_CATALOG_DIR (tools/paths.py)

# Scheduler smoke tests: real records, but not science. They are adopted only
# on request, since they would otherwise clutter every search.
_SMOKE_PATTERNS = (
    r"smoke",
    r"hello[-_]?world",
)

# Trailing tokens a restart or attempt adds to a directory name. Stripping them
# lets ".../k1" and ".../k1/run" land in the same simulation.
_DIRECTORY_NOISE = ("run", "runs")

# Parent directories that are storage locations rather than campaign names, so
# prefixing a key with them would add length without meaning.
_GENERIC_PARENTS = {
    "xgc_runs",
    "runs",
    "run",
    "scratch",
    "codex_work",
    "work",
    "cases",
    "tmp",
}


def _is_ambiguous_leaf(name: str) -> bool:
    """Whether a directory name is too generic to identify a simulation.

    Variant labels like "k1", "off", or "case3" are meaningful only beside the
    campaign directory they sit in.
    """
    return len(name) <= 4 or bool(re.fullmatch(r"(?:off|on|base|ref|test)[0-9_]*", name))


class RunAdoptionError(RuntimeError):
    """The adoption request cannot be satisfied safely."""


def _looks_like_smoke_test(record: dict[str, Any]) -> bool:
    text = " ".join(
        str(record.get(key) or "")
        for key in ("run_id", "job_name", "remote_run_dir", "case_dir")
    ).lower()
    return any(re.search(pattern, text) for pattern in _SMOKE_PATTERNS)


def _simulation_directory(record: dict[str, Any]) -> str:
    """The directory that identifies the simulation this run belongs to.

    ``case_dir`` is preferred: it is where the input lived, and restarts of one
    simulation share it. ``remote_run_dir`` is the fallback for older records.
    """
    raw = record.get("case_dir") or record.get("remote_run_dir") or ""
    directory = str(raw).rstrip("/")
    if not directory:
        return ""
    # ".../dev_reduce_adia/k1/run" and ".../dev_reduce_adia/k1" are the same
    # simulation; the trailing "run" is a working subdirectory, not a name.
    head, _, tail = directory.rpartition("/")
    if head and tail.lower() in _DIRECTORY_NOISE:
        return head
    return directory


def _record_id_for(directory: str, machine: str) -> str:
    """Key an adopted simulation by its directory name.

    A leaf name is often a bare variant label ("k1", "off") that only means
    something next to its campaign directory, so those are qualified with the
    parent ("dev_reduce_adia_k1"). Generic containers such as "xgc-runs" or
    "codex_work" add nothing and are not used.

    Collisions across machines are resolved by ``derive_groups``, which can see
    every group at once.
    """
    path = Path(directory)
    name = catalog.slugify(path.name)
    parent = catalog.slugify(path.parent.name)
    if name and _is_ambiguous_leaf(name) and parent and parent not in _GENERIC_PARENTS:
        name = f"{parent}_{name}"
    name = name or catalog.slugify(machine)
    if not name:
        raise RunAdoptionError(f"cannot derive a record id from {directory!r}")
    if name[0].isdigit():
        # Keys must start with a letter; keep the digits rather than drop them.
        name = f"r{name}"
    return name


def _load_orphans(
    runs_dir: Path,
    *,
    include_smoke_tests: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read provenance records that no catalog record already claims."""
    orphans: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return orphans, skipped

    for provenance_class, path in prov.iter_records(runs_dir):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"provenance": str(path), "reason": f"unreadable: {exc}"})
            continue
        if not isinstance(record, dict):
            skipped.append({"provenance": str(path), "reason": "not a JSON object"})
            continue

        run_id = record.get("run_id")
        if not run_id:
            skipped.append({"provenance": str(path), "reason": "no run_id"})
            continue
        if record.get("simulation_id"):
            skipped.append(
                {
                    "run_id": run_id,
                    "reason": (
                        f"already linked to simulation "
                        f"{record['simulation_id']!r}"
                    ),
                }
            )
            continue
        if not include_smoke_tests and _looks_like_smoke_test(record):
            skipped.append(
                {"run_id": run_id, "reason": "scheduler smoke test; use --include-smoke-tests"}
            )
            continue
        if not _simulation_directory(record):
            skipped.append(
                {"run_id": run_id, "reason": "no case_dir or remote_run_dir to group by"}
            )
            continue

        record["_provenance_path"] = path
        record["_provenance_class"] = provenance_class
        orphans.append(record)

    return orphans, skipped


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _run_entry(record: dict[str, Any]) -> dict[str, Any]:
    dryrun = record.get("dryrun") or {}
    return {
        "run_id": record["run_id"],
        "machine": record.get("machine"),
        "job_id": record.get("job_id"),
        "submitted_at": record.get("submitted_at"),
        "provenance": _display_path(record["_provenance_path"]),
        "remote_run_dir": record.get("remote_run_dir"),
        "state": record.get("state"),
        "exit_code": record.get("exit_code"),
        "input": record.get("input"),
        "dryrun_ok": dryrun.get("ok") if isinstance(dryrun, dict) else None,
    }


def _first(records: list[dict[str, Any]], key: str) -> Any:
    """The first non-empty value across a simulation's runs."""
    for record in records:
        value = record.get(key)
        if value:
            return value
    return None


def _first_nested(records: list[dict[str, Any]], key: str, subkey: str) -> Any:
    """The first non-empty nested value, e.g. the XGC commit a run recorded."""
    for record in records:
        section = record.get(key)
        if isinstance(section, dict) and section.get(subkey):
            return section[subkey]
    return None


def derive_groups(
    orphans: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group orphan runs into prospective simulations, one per directory."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in orphans:
        key = (str(record.get("machine") or ""), _simulation_directory(record))
        grouped.setdefault(key, []).append(record)

    # Two machines can use the same directory name; disambiguate only those,
    # so the common case keeps the short readable key.
    base_names = {
        (machine, directory): _record_id_for(directory, machine)
        for machine, directory in grouped
    }
    counts = Counter(base_names.values())
    collisions = {name for name, count in counts.items() if count > 1}

    groups: list[dict[str, Any]] = []
    for key, records in sorted(grouped.items()):
        machine, directory = key
        records.sort(key=lambda item: str(item.get("submitted_at") or ""))
        base = base_names[key]
        record_id = f"{base}_{catalog.slugify(machine)}" if base in collisions else base
        groups.append(
            {
                "record_id": record_id,
                "machine": machine,
                "directory": directory,
                "runs": [_run_entry(record) for record in records],
                "run_ids": [record["run_id"] for record in records],
                "provenance_classes": sorted(
                    {record["_provenance_class"] for record in records}
                ),
                "xgc_commit": _first_nested(records, "xgc", "commit"),
                "input_sha256": _first(records, "input_sha256"),
                "input": _first(records, "input"),
                "submitted_at": _first(records, "submitted_at"),
            }
        )
    return groups


def _status_for(group: dict[str, Any]) -> str:
    """What an adopted simulation's status can honestly claim.

    Provenance records a submission, not an outcome: these runs predate the
    status write-back, so their scheduler result was never captured. Claiming
    ``completed`` would be inventing a result. ``submitted`` is what is known,
    and ``xgc-sim status`` lets a human correct it.
    """
    states = {run.get("state") for run in group["runs"] if run.get("state")}
    if states == {"completed"}:
        return "completed"
    if "failed" in states:
        return "failed"
    return "submitted"


def build_record(group: dict[str, Any]) -> dict[str, Any]:
    """Turn one group of runs into a catalog record. Writes nothing."""
    directory_name = Path(group["directory"]).name
    simulation_tag = catalog.infer_simulation_tag(
        group["directory"], fallback=group["record_id"]
    )
    record = catalog.new_record(
        group["record_id"],
        simulation_tag=simulation_tag,
        sub_tag=directory_name,
        machines=[group["machine"]] if group["machine"] else [],
        short_name=directory_name,
        directory_name=directory_name,
        tags=["adopted"],
        status=_status_for(group),
        origin="adopted",
        note="",
    )
    record["runs"] = group["runs"]
    if group["machine"]:
        record["remote_dirs"] = {group["machine"]: group["directory"]}
    record["input_sha256"] = group["input_sha256"]
    if isinstance(group.get("input"), dict):
        record["input"] = group["input"]
    record["started_on"] = str(group["submitted_at"] or "")[:10]
    record["xgc_commit"] = group["xgc_commit"]
    # A tag inferred from a historical directory is not a registry reservation.
    record["simulation_id"] = None
    record["number"] = None
    # Adopted records carry no reserved number. Say so explicitly rather than
    # leaving a null that looks like missing data.
    record["adoption"] = {
        "source": "runs/ provenance predating the catalog",
        "grouped_by": "working directory",
        "directory": group["directory"],
        "provenance_classes": group["provenance_classes"],
        "numbered": False,
        "detail": (
            "adopted from existing provenance; never assigned a sequence "
            "number from kb/simulation_registry.yaml"
        ),
    }
    return record


def plan_adoption(
    *,
    runs_dir: Path | None = None,
    catalog_dir: Path | None = None,
    include_smoke_tests: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Work out which runs would be adopted, and as what. Writes nothing."""
    orphans, skipped = _load_orphans(
        runs_dir or RUNS_DIR, include_smoke_tests=include_smoke_tests
    )
    create: list[dict[str, Any]] = []
    update: list[dict[str, Any]] = []
    existing_records = list(catalog.iter_records(catalog_dir=catalog_dir))
    existing_by_id = {
        str(record.get("record_id")): record for record in existing_records
    }
    existing_by_identity = {
        catalog.record_identity(record): record for record in existing_records
    }

    for group in derive_groups(orphans):
        record = build_record(group)
        identity = catalog.record_identity(record)
        existing = existing_by_identity.get(identity)
        key_owner = existing_by_id.get(group["record_id"])
        if existing is None and key_owner is not None:
            skipped.append(
                {
                    "record_id": group["record_id"],
                    "reason": (
                        f"record key already represents simulation_tag + sub_tag "
                        f"{catalog.record_identity(key_owner)!r}"
                    ),
                    "run_ids": group["run_ids"],
                }
            )
            continue
        if existing is not None:
            if existing.get("origin") != "adopted":
                skipped.append(
                    {
                        "record_id": group["record_id"],
                        "reason": (
                            f"a record with origin {existing.get('origin')!r} "
                            f"already owns this key"
                        ),
                        "run_ids": group["run_ids"],
                    }
                )
                continue
            if not overwrite:
                skipped.append(
                    {
                        "record_id": group["record_id"],
                        "reason": "already adopted; pass overwrite=True to refresh",
                        "run_ids": group["run_ids"],
                    }
                )
                continue
            record["record_id"] = existing["record_id"]

        entry = {
            "record_id": record["record_id"],
            "simulation_tag": record["simulation_tag"],
            "sub_tag": record["sub_tag"],
            "machine": group["machine"],
            "directory": group["directory"],
            "runs": len(group["runs"]),
            "run_ids": group["run_ids"],
            "record": record,
        }
        (update if existing is not None else create).append(entry)

    return {
        "orphan_runs": len(orphans),
        "create": create,
        "update": update,
        "skip": skipped,
        "counts": {
            "create": len(create),
            "update": len(update),
            "skip": len(skipped),
            "runs_adopted": sum(entry["runs"] for entry in create + update),
        },
        "runs_dir": str(runs_dir or RUNS_DIR),
        "writes": False,
    }


def adopt_runs(
    *,
    runs_dir: Path | None = None,
    catalog_dir: Path | None = None,
    include_smoke_tests: bool = False,
    overwrite: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Write the adopted records after explicit confirmation."""
    plan = plan_adoption(
        runs_dir=runs_dir,
        catalog_dir=catalog_dir,
        include_smoke_tests=include_smoke_tests,
        overwrite=overwrite,
    )
    if not confirm:
        plan["adopted"] = False
        plan["reason"] = (
            "confirmation required; review the groups and resubmit with confirm=True"
        )
        return plan

    written: list[str] = []
    for entry in plan["create"] + plan["update"]:
        catalog.write_record(entry["record"], catalog_dir=catalog_dir)
        written.append(entry["record_id"])
    for entry in plan["create"] + plan["update"]:
        entry.pop("record", None)

    plan["adopted"] = True
    plan["written"] = written
    plan["writes"] = True
    return plan
