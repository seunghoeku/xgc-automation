"""The simulation catalog: one durable record per scientific simulation.

Historically each simulation was managed by hand: a human picked the next
number, made a directory named after it, typed the provenance into a
spreadsheet, and archived the output to HPSS under the same number. That works,
but the number, the spreadsheet row, the run directory, and the scheduler job
were only ever connected inside the researcher's head.

This module keeps the number and replaces the bookkeeping. Each simulation owns
one YAML file:

    kb/simulations/<record_id>.yaml

The record is the single source of truth. It accumulates the scheduler runs
belonging to that simulation, the status of the work, the archive location, and
the human's own note. ``tools/simulation_registry.py`` still allocates the
number; ``tools/hpc_runner.py`` appends runs as they are submitted; the
``xgc-sim`` CLI reads and searches; ``tools/sheet_sync.py`` mirrors the catalog
to Google Sheets. None of that requires an LLM.

Design constraints worth stating, because they shaped the schema:

* Legacy numbers and modern tags may be reused. The logical identity is
  ``simulation_tag + sub_tag``; the record ID is only the unique storage key.
* Records are plain YAML, one file per catalog item, so git history shows what
  changed about an item rather than one perpetually-conflicting index.
* Writes are atomic and field-scoped. Automated updates must never clobber the
  human's ``note``.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Overridable so a second checkout, or a test, can point at its own catalog
# without editing code. Every function also takes an explicit catalog_dir.
CATALOG_DIR = Path(
    os.environ.get("XGC_SIMULATION_CATALOG", PROJECT_ROOT / "kb" / "simulations")
)

SCHEMA_VERSION = 2

# Lifecycle of a simulation, in the order work normally progresses. `failed`
# and `abandoned` are terminal off-ramps rather than a stage of progress.
STATUSES = (
    "reserved",     # number allocated, nothing staged yet
    "staged",       # case files placed in the canonical directory
    "submitted",    # at least one scheduler job accepted
    "running",      # a job is executing
    "interrupted",  # last job hit the wall clock; restart to continue
    "completed",    # the last job ran to a clean finish
    "failed",       # the last job ended in error
    "analyzed",     # reduced products exist
    "archived",     # output moved to long-term storage (HPSS)
    "abandoned",    # deliberately dropped; number stays burned
)

# Statuses a scheduler result may set on its own. Everything else in STATUSES
# is a human judgement about the science.
_OUTCOME_STATUSES = {"completed", "failed", "interrupted"}

# How one run's scheduler state maps onto the simulation, when that run is the
# newest one. `timeout` becomes `interrupted` rather than `failed`: a long XGC
# run is routinely sized to hit the wall clock and restart from its
# checkpoint, so it means "not finished yet", not "broken".
#
# `cancelled` is absent on purpose. Cancelling one attempt says nothing about
# whether the science was abandoned, and only a human knows which it was.
_RUN_STATE_TO_STATUS = {
    "completed": "completed",
    "failed": "failed",
    "timeout": "interrupted",
}

# Statuses that represent a human's conclusion about the science. A later
# scheduler result must never overwrite one of these.
_HUMAN_JUDGEMENT_STATUSES = {"analyzed", "archived", "abandoned"}

# Legacy notebook outcomes, produced by case_promotion.classify_outcome().
LEGACY_OUTCOMES = ("success", "failed", "inconclusive", "unknown")

# How a record came to exist, which bounds what it can claim. Only `automation`
# records were run through these tools and carry verified provenance;
# `adopted` records have real runs but no recorded outcome, and `notebook`
# records are the researcher's evidence with no provenance at all.
ORIGINS = ("automation", "adopted", "notebook")

# A catalog key: lowercase alphanumerics and underscores, starting with a
# letter. Deliberately broader than the numbered schemes, because adopted runs
# are keyed by their working-directory name (see tools/run_adoption.py), which
# never had a number. The pattern's real job is to reject anything that could
# escape the catalog directory: no dots, slashes, or separators of any kind.
_RECORD_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MODERN_ID_RE = re.compile(r"^n([0-9]{3,})([a-z]{2}(?:_[a-z]{2})*)$")
_KNOWN_MACHINE_CODE_RE = r"(?:pe|po|au|do|fr|su|st|fl)"

# Fields the human owns. Automated writers must leave them alone unless the
# human explicitly asked for the change.
HUMAN_FIELDS = ("note",)


class SimulationCatalogError(RuntimeError):
    """A catalog record is invalid or the requested operation is unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ascii_words(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return [part.lower() for part in re.findall(r"[A-Za-z0-9]+", text)]


def slugify(value: str) -> str:
    """ASCII, lowercase, underscore-joined. Used for the file name suffix."""
    return "_".join(_ascii_words(value))


def directory_sub_tag(directory_name: str | None, fallback: str) -> str:
    """Return the default sub-tag: the final component of the directory name."""
    value = str(directory_name or "").strip().rstrip("/")
    return Path(value).name if value else str(fallback).strip()


def infer_simulation_tag(value: str, *, fallback: str | None = None) -> str:
    """Infer a shot-like tag such as ``n645pe`` from a directory or record key.

    The tag is deliberately separate from ``record_id``. Several catalog items
    may share it, provided their sub-tags differ.
    """
    path = Path(str(value or "").strip().rstrip("/"))
    name = path.name.lower()
    match = None
    # A manual run often lives one level below its shot directory, e.g.
    # n644pe_TCV_NT_PT_ES/NT1_psi_1.0. Prefer the nearest modern shot component.
    for component in reversed(path.parts):
        match = re.match(
            rf"^(n[0-9]+{_KNOWN_MACHINE_CODE_RE}"
            rf"(?:_{_KNOWN_MACHINE_CODE_RE})*)",
            component.lower(),
        )
        if match:
            break
    if match is None:
        match = re.match(r"^([a-z]+[0-9]+(?:[a-z]{2})?)", name)
    candidate = match.group(1) if match else str(fallback or name)
    tag = slugify(candidate)
    if not tag:
        raise SimulationCatalogError("simulation tag must not be empty")
    return tag


def identity_key(simulation_tag: str, sub_tag: str) -> tuple[str, str]:
    """Canonical comparison key for the user-visible two-part identity."""
    simulation = slugify(simulation_tag)
    # Directory names are case-sensitive and punctuation can be meaningful.
    # Normalize the grouping tag, but compare the sub-tag itself after trimming.
    sub = str(sub_tag).strip()
    if not simulation:
        raise SimulationCatalogError("simulation tag must not be empty")
    if not sub:
        raise SimulationCatalogError("sub tag must not be empty")
    return simulation, sub


def record_identity(record: dict[str, Any]) -> tuple[str, str]:
    """Return an identity for new records and pre-v2 records alike."""
    record_id = str(record.get("record_id") or "")
    legacy = record.get("legacy") or {}
    legacy_shot = legacy.get("shot") if isinstance(legacy, dict) else None
    simulation_tag = record.get("simulation_tag") or record.get("simulation_id")
    if not simulation_tag and legacy_shot:
        simulation_tag = infer_simulation_tag(str(legacy_shot), fallback=record_id)
    if not simulation_tag:
        simulation_tag = infer_simulation_tag(
            record.get("directory_name") or record_id, fallback=record_id
        )
    sub_tag = record.get("sub_tag") or directory_sub_tag(
        record.get("directory_name"), record_id
    )
    return identity_key(str(simulation_tag), str(sub_tag))


def identity_record_id(
    simulation_tag: str,
    sub_tag: str,
    *,
    catalog_dir: Path | None = None,
    reserved_ids: Iterable[str] | None = None,
) -> str:
    """Choose a safe storage key for a two-part identity.

    The first item for a simulation tag keeps the short historical key
    (``n645pe``). Additional sub-tags use ``n645pe__<sub-tag>``. The storage
    key is an implementation detail; equality is always the two-part identity.
    """
    simulation, sub = identity_key(simulation_tag, sub_tag)
    occupied = {validate_record_id(value) for value in (reserved_ids or [])}
    base = validate_record_id(simulation)
    if base not in occupied and not record_path(base, catalog_dir=catalog_dir).exists():
        return base
    sub_slug = slugify(sub)
    candidate = validate_record_id(f"{simulation}__{sub_slug}")
    if candidate not in occupied:
        existing = find_record(candidate, catalog_dir=catalog_dir)
        if existing is None or record_identity(existing) == (simulation, sub):
            return candidate

    # Two distinct case-sensitive directory names can slugify to the same file
    # key (for example A-B and A_B). Keep both items by adding a stable suffix.
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:8]
    qualified = validate_record_id(f"{candidate}_{digest}")
    if qualified in occupied:
        raise SimulationCatalogError(
            f"record id collision for simulation_tag={simulation_tag!r}, "
            f"sub_tag={sub_tag!r}: {qualified!r} is already reserved"
        )
    existing = find_record(qualified, catalog_dir=catalog_dir)
    if existing is None or record_identity(existing) == (simulation, sub):
        return qualified
    raise SimulationCatalogError(
        f"record id collision for simulation_tag={simulation_tag!r}, "
        f"sub_tag={sub_tag!r}: {qualified!r} already represents another item"
    )


def validate_record_id(record_id: str) -> str:
    """Accept a catalog key, rejecting anything that could escape the directory.

    Registered simulations use ``n645pe`` or ``n647pe_po``. Legacy notebook
    shots use ``ti204`` or ``j86``. Adopted runs use their directory name, such
    as ``tcv_xgca_reduce_adia_k1``.
    """
    value = str(record_id).strip().lower()
    if not value:
        raise SimulationCatalogError("record id must not be empty")
    if not _RECORD_ID_RE.fullmatch(value):
        raise SimulationCatalogError(
            f"invalid simulation record id {record_id!r}; expected lowercase "
            f"letters, digits, and underscores, starting with a letter -- for "
            f"example 'n645pe', 'ti204', or 'tcv_xgca_reduce_adia_k1'"
        )
    return value


def record_number(record_id: str) -> int | None:
    """The sequence number in a record id, or None when it has none.

    Only the numbered schemes have one. An adopted run keyed by its directory
    name may well contain digits (``..._k0p1``) without ever having been
    assigned a number, and must not appear to own one.
    """
    value = validate_record_id(record_id)
    match = re.fullmatch(r"[a-z]+([0-9]+)(?:[a-z]{2})?(?:_[a-z]{2})*", value)
    return int(match.group(1)) if match else None


def is_modern_id(record_id: str) -> bool:
    """True when the id follows the current n<NUMBER><MACHINE CODE> scheme."""
    return bool(_MODERN_ID_RE.fullmatch(str(record_id).strip().lower()))


def record_path(record_id: str, *, catalog_dir: Path | None = None) -> Path:
    """Where a record lives. One file per simulation, keyed by record id."""
    directory = catalog_dir or CATALOG_DIR
    return directory / f"{validate_record_id(record_id)}.yaml"


def _normalize_list(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_tags(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        tag = "-".join(_ascii_words(value))
        if tag and tag not in out:
            out.append(tag)
    return out


def validate_status(status: str) -> str:
    value = str(status).strip().lower()
    if value not in STATUSES:
        raise SimulationCatalogError(
            f"unknown status {status!r}; expected one of {', '.join(STATUSES)}"
        )
    return value


def validate_origin(origin: str) -> str:
    value = str(origin).strip().lower()
    if value not in ORIGINS:
        raise SimulationCatalogError(
            f"unknown origin {origin!r}; expected one of {', '.join(ORIGINS)}"
        )
    return value


def new_record(
    record_id: str,
    *,
    simulation_tag: str | None = None,
    sub_tag: str | None = None,
    machines: Iterable[str] | None = None,
    short_name: str = "",
    directory_name: str | None = None,
    tags: Iterable[str] | None = None,
    status: str = "reserved",
    case: str | None = None,
    origin: str = "automation",
    note: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Build a catalog record in canonical field order. Writes nothing.

    ``origin`` bounds what the record may claim: only ``automation`` records
    were run through this toolchain and carry verified provenance. See
    ``ORIGINS``.
    """
    record_id = validate_record_id(record_id)
    resolved_directory = str(directory_name or record_id)
    if simulation_tag:
        resolved_simulation_tag = slugify(simulation_tag)
    elif is_modern_id(record_id):
        resolved_simulation_tag = record_id
    else:
        resolved_simulation_tag = infer_simulation_tag(record_id, fallback=record_id)
    resolved_sub_tag = str(
        sub_tag or directory_sub_tag(resolved_directory, record_id)
    ).strip()
    identity_key(resolved_simulation_tag, resolved_sub_tag)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "simulation_id": (
            resolved_simulation_tag if is_modern_id(resolved_simulation_tag) else None
        ),
        "simulation_tag": resolved_simulation_tag,
        "sub_tag": resolved_sub_tag,
        "number": record_number(resolved_simulation_tag),
        "short_name": slugify(short_name),
        "machines": _normalize_list(machines),
        "status": validate_status(status),
        "origin": validate_origin(origin),
        "case": case,
        "directory_name": resolved_directory,
        "tags": _normalize_tags(tags),
        "remote_dirs": {},
        "input_sha256": None,
        "runs": [],
        "archive": {"system": None, "path": None, "archived_at": None},
        "note": str(note or ""),
        "created_at": _now(),
        "updated_at": _now(),
    }
    record.update(extra)
    return record


def _coerce_record(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SimulationCatalogError(f"{path} does not contain a mapping")
    if "record_id" not in data:
        raise SimulationCatalogError(f"{path} has no record_id")
    return data


def read_record(record_id: str, *, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Load one record. Raises when it does not exist."""
    path = record_path(record_id, catalog_dir=catalog_dir)
    if not path.is_file():
        raise SimulationCatalogError(f"no catalog record for {record_id!r} ({path})")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SimulationCatalogError(f"cannot read {path}: {exc}") from exc
    return _coerce_record(data, path)


def find_record(record_id: str, *, catalog_dir: Path | None = None) -> dict[str, Any] | None:
    """Load one record, or None when it does not exist.

    Absence is not an error, but a malformed record or an invalid id still is:
    silently returning None for a corrupt file would hide the loss.
    """
    path = record_path(record_id, catalog_dir=catalog_dir)
    if not path.is_file():
        return None
    return read_record(record_id, catalog_dir=catalog_dir)


def find_record_by_identity(
    simulation_tag: str,
    sub_tag: str,
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Find the one item whose simulation and sub tags both match."""
    wanted = identity_key(simulation_tag, sub_tag)
    matches = [
        record
        for record in iter_records(catalog_dir=catalog_dir)
        if record_identity(record) == wanted
    ]
    if len(matches) > 1:
        ids = ", ".join(str(record.get("record_id")) for record in matches)
        raise SimulationCatalogError(
            f"duplicate catalog identity simulation_tag={simulation_tag!r}, "
            f"sub_tag={sub_tag!r}: {ids}"
        )
    return matches[0] if matches else None


def write_record(
    record: dict[str, Any],
    *,
    catalog_dir: Path | None = None,
    overwrite: bool = True,
) -> Path:
    """Atomically persist a record.

    The temp-file-then-replace dance matters: a catalog file half-written by an
    interrupted submission would lose the run history it was meant to protect.
    """
    record_id = validate_record_id(record.get("record_id", ""))
    validate_status(record.get("status", "reserved"))
    record_identity(record)
    path = record_path(record_id, catalog_dir=catalog_dir)
    if path.exists() and not overwrite:
        raise SimulationCatalogError(f"catalog record already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = _now()
    fd, temp_name = tempfile.mkstemp(prefix=f".{record_id}-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(record, stream, sort_keys=False, allow_unicode=True)
        Path(temp_name).replace(path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def create_record(
    record_id: str,
    *,
    catalog_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a record, refusing a duplicate two-part identity."""
    record = new_record(record_id, **kwargs)
    simulation_tag, sub_tag = record_identity(record)
    existing = find_record_by_identity(
        simulation_tag, sub_tag, catalog_dir=catalog_dir
    )
    if existing is not None:
        raise SimulationCatalogError(
            f"catalog item already exists for simulation_tag={simulation_tag!r}, "
            f"sub_tag={sub_tag!r}: {existing.get('record_id')}"
        )
    write_record(record, catalog_dir=catalog_dir, overwrite=False)
    return record


def update_record(
    record_id: str,
    updates: dict[str, Any],
    *,
    catalog_dir: Path | None = None,
    allow_human_fields: bool = False,
) -> dict[str, Any]:
    """Apply field updates to an existing record.

    Automated callers may not touch ``note`` and the other human-owned fields;
    that is what keeps the spreadsheet round trip safe.
    """
    record = read_record(record_id, catalog_dir=catalog_dir)
    if not allow_human_fields:
        blocked = sorted(set(updates) & set(HUMAN_FIELDS))
        if blocked:
            raise SimulationCatalogError(
                f"fields {blocked} are human-owned; set allow_human_fields=True "
                f"only for an explicit human edit"
            )
    if "status" in updates:
        updates = dict(updates)
        updates["status"] = validate_status(updates["status"])
    if "tags" in updates:
        updates = dict(updates)
        updates["tags"] = _normalize_tags(updates["tags"])
    record.update(updates)
    write_record(record, catalog_dir=catalog_dir)
    return record


def set_note(
    record_id: str,
    note: str,
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Set the human note. The one field a person is expected to write."""
    return update_record(
        record_id,
        {"note": str(note)},
        catalog_dir=catalog_dir,
        allow_human_fields=True,
    )


def set_archive(
    record_id: str,
    *,
    system: str,
    path: str,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Record where a simulation's output was archived (for example HPSS).

    The catalog stores the location only. Driving ``htar``/``hsi`` is out of
    scope here; the point is that the number-to-tape mapping stops living in a
    human's memory.
    """
    return update_record(
        record_id,
        {
            "archive": {
                "system": str(system),
                "path": str(path),
                "archived_at": _now(),
            },
            "status": "archived",
        },
        catalog_dir=catalog_dir,
    )


def attach_run(
    record_id: str,
    run: dict[str, Any],
    *,
    catalog_dir: Path | None = None,
    status: str | None = "submitted",
) -> dict[str, Any]:
    """Link a scheduler submission to its simulation.

    Called by ``hpc_runner.submit`` right after a job is accepted, so the number
    and the job stop being connected only by hand. Re-attaching the same
    ``run_id`` updates it in place rather than duplicating it, which makes the
    call safe to repeat.
    """
    record = read_record(record_id, catalog_dir=catalog_dir)
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        raise SimulationCatalogError("a linked run needs a run_id")

    entry = {
        "run_id": run_id,
        "machine": run.get("machine"),
        "job_id": run.get("job_id"),
        "submitted_at": run.get("submitted_at") or _now(),
        "provenance": run.get("provenance"),
        "remote_run_dir": run.get("remote_run_dir"),
        "state": run.get("state"),
        "exit_code": run.get("exit_code"),
        "input": run.get("input"),
        "dryrun_parallel": run.get("dryrun_parallel"),
    }

    runs = list(record.get("runs") or [])
    for index, existing in enumerate(runs):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            merged = dict(existing)
            merged.update({k: v for k, v in entry.items() if v is not None})
            runs[index] = merged
            break
    else:
        runs.append(entry)
    record["runs"] = runs

    machine = run.get("machine")
    remote_dir = run.get("remote_run_dir")
    if machine and remote_dir:
        remote_dirs = dict(record.get("remote_dirs") or {})
        remote_dirs[str(machine)] = str(remote_dir)
        record["remote_dirs"] = remote_dirs
    if run.get("input_sha256"):
        record["input_sha256"] = run["input_sha256"]
    if isinstance(run.get("input"), dict):
        record["input"] = run["input"]
    if run.get("case") and not record.get("case"):
        record["case"] = run["case"]
    # Reproduction evidence gathered at submission (hpc_runner): hashes of the
    # mesh/equilibrium/profile files the input names, and the build
    # environment. The newest submission describes the simulation as it now is.
    for key in ("referenced_inputs", "build_environment", "input_backup"):
        if isinstance(run.get(key), dict):
            record[key] = run[key]
    if status and record.get("status") not in _HUMAN_JUDGEMENT_STATUSES:
        # Submitting a restart moves the simulation back into flight, but it
        # must not undo a conclusion the human already recorded.
        record["status"] = validate_status(status)

    write_record(record, catalog_dir=catalog_dir)
    return record


def _run_order_key(run: dict[str, Any]) -> tuple[str, str]:
    """Sort runs oldest-first within a simulation.

    A restart chain is ordered by submission time; ``run_id`` is the tiebreaker
    because it is itself a timestamp.
    """
    return (str(run.get("submitted_at") or ""), str(run.get("run_id") or ""))


def derive_status(record: dict[str, Any]) -> str:
    """Work out a simulation's status from *all* of its runs.

    A simulation is one scientific effort that may take many scheduler jobs:
    hit the wall clock, restart from the checkpoint, repeat. So the status
    cannot come from whichever run happened to be polled last. It comes from
    the newest run that actually reported an outcome.

    A failure early in a chain that was then restarted and finished is history,
    not the current state -- which is why this reads the latest run rather than
    the worst one.
    """
    current = str(record.get("status") or "reserved")
    if current in _HUMAN_JUDGEMENT_STATUSES:
        # The human has drawn a conclusion; a scheduler result cannot undo it.
        return current

    runs = [run for run in (record.get("runs") or []) if isinstance(run, dict)]
    if not runs:
        return current

    ordered = sorted(runs, key=_run_order_key)
    if any(run.get("state") == "running" for run in ordered):
        return "running"

    for run in reversed(ordered):
        status = _RUN_STATE_TO_STATUS.get(str(run.get("state") or ""))
        if status:
            return status

    # Runs exist but none has reported an outcome yet: still in flight.
    return "running" if current == "running" else "submitted"


def update_run_state(
    record_id: str,
    run_id: str,
    *,
    state: str,
    exit_code: int | None = None,
    terminal: bool = False,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Record one job's observed state and re-derive the simulation's status.

    ``terminal`` is accepted for callers that already know whether the state
    ends the job, but the status itself is derived from every run rather than
    from this one, so a restart chain is read as a whole.
    """
    record = read_record(record_id, catalog_dir=catalog_dir)
    runs = list(record.get("runs") or [])
    for index, existing in enumerate(runs):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            updated = dict(existing)
            updated["state"] = state
            if exit_code is not None:
                updated["exit_code"] = exit_code
            updated["state_checked_at"] = _now()
            runs[index] = updated
            break
    else:
        raise SimulationCatalogError(
            f"run {run_id!r} is not linked to simulation {record_id!r}"
        )
    record["runs"] = runs
    record["status"] = derive_status(record)
    write_record(record, catalog_dir=catalog_dir)
    return record


def set_run_field(
    record_id: str,
    run_id: str,
    key: str,
    value: Any,
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Set one field on one linked run (e.g. the parallel dryrun's verdict)."""
    record = read_record(record_id, catalog_dir=catalog_dir)
    runs = list(record.get("runs") or [])
    for index, existing in enumerate(runs):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            updated = dict(existing)
            updated[key] = value
            runs[index] = updated
            break
    else:
        raise SimulationCatalogError(
            f"run {run_id!r} is not linked to simulation {record_id!r}"
        )
    record["runs"] = runs
    write_record(record, catalog_dir=catalog_dir)
    return record


def set_run_build(
    record_id: str,
    run_id: str,
    build: dict[str, Any],
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Record the XGC build banner captured from one run's stdout.

    The banner (parsed by ``tools.build_banner``) is what the binary said
    about itself at execution time -- commit, build date, dirty state -- and
    therefore outranks what the submission plan assumed. Setting it does not
    touch the run's scheduler state or the simulation's status.
    """
    record = read_record(record_id, catalog_dir=catalog_dir)
    runs = list(record.get("runs") or [])
    for index, existing in enumerate(runs):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            updated = dict(existing)
            updated["build"] = build
            runs[index] = updated
            break
    else:
        raise SimulationCatalogError(
            f"run {run_id!r} is not linked to simulation {record_id!r}"
        )
    record["runs"] = runs
    write_record(record, catalog_dir=catalog_dir)
    return record


def iter_records(*, catalog_dir: Path | None = None) -> Iterator[dict[str, Any]]:
    """Yield every catalog record, sorted by number then id."""
    directory = catalog_dir or CATALOG_DIR
    if not directory.is_dir():
        return
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("record_id"):
            records.append(data)
    records.sort(key=lambda item: (item.get("number") or 0, str(item.get("record_id"))))
    yield from records


def summarize(record: dict[str, Any]) -> dict[str, Any]:
    """A compact view: what a list row or spreadsheet line needs."""
    runs = [run for run in (record.get("runs") or []) if isinstance(run, dict)]
    latest = runs[-1] if runs else {}
    machines = record.get("machines") or []
    simulation_tag, normalized_sub_tag = record_identity(record)
    input_provenance = record.get("input") or {}
    input_parameters = input_provenance.get("parameters") or {}
    input_derived = input_provenance.get("derived") or {}
    return {
        "record_id": record.get("record_id"),
        "simulation_tag": record.get("simulation_tag") or simulation_tag,
        "sub_tag": record.get("sub_tag")
        or directory_sub_tag(record.get("directory_name"), normalized_sub_tag),
        "number": record.get("number"),
        "short_name": record.get("short_name"),
        "machines": machines,
        "machine": machines[0] if machines else None,
        "status": record.get("status"),
        "origin": record.get("origin"),
        "case": record.get("case"),
        "tags": record.get("tags") or [],
        "field_model": input_derived.get("field_model") or "unknown",
        "marker_model": input_derived.get("marker_model") or "unknown",
        "species": input_derived.get("species"),
        "sml_node_file": input_parameters.get("sml_node_file"),
        "sml_electron_on": input_parameters.get("sml_electron_on"),
        "eq_filename": input_parameters.get("eq_filename"),
        "ptl_marker_type": input_parameters.get("ptl_marker_type"),
        "runs": len(runs),
        "latest_run_id": latest.get("run_id"),
        "latest_job_id": latest.get("job_id"),
        "latest_state": latest.get("state"),
        "submitted_at": latest.get("submitted_at"),
        "archive": (record.get("archive") or {}).get("path"),
        "note": record.get("note") or "",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _haystack(record: dict[str, Any]) -> str:
    """Everything a free-text search should look at, flattened once."""
    parts: list[Any] = [
        record.get("record_id"),
        record.get("simulation_id"),
        record.get("simulation_tag"),
        record.get("sub_tag"),
        record.get("number"),
        record.get("short_name"),
        record.get("status"),
        record.get("origin"),
        record.get("case"),
        record.get("note"),
        record.get("description"),
        record.get("geometry"),
        record.get("code"),
        record.get("directory_name"),
        (record.get("archive") or {}).get("path"),
    ]
    parts.extend(record.get("machines") or [])
    parts.extend(record.get("tags") or [])
    parts.extend((record.get("remote_dirs") or {}).values())
    input_provenance = record.get("input") or {}
    if isinstance(input_provenance, dict):
        parameters = input_provenance.get("parameters") or {}
        derived = input_provenance.get("derived") or {}
        if isinstance(parameters, dict):
            parts.extend(parameters.values())
        if isinstance(derived, dict):
            parts.extend(derived.values())
    for run in record.get("runs") or []:
        if isinstance(run, dict):
            parts.extend([run.get("run_id"), run.get("job_id"), run.get("remote_run_dir")])
            build = run.get("build")
            if isinstance(build, dict):
                parts.extend(
                    [build.get("commit"), build.get("commit_subject"), build.get("build_type")]
                )
    legacy = record.get("legacy") or {}
    if isinstance(legacy, dict):
        parts.extend([legacy.get("shot"), legacy.get("sheet"), legacy.get("data")])
    return "\n".join(str(part) for part in parts if part not in (None, "")).lower()


def _query_terms(query: str) -> list[str]:
    """Split a query into terms, ignoring the separators people vary on.

    Names are written inconsistently across the catalog's three sources:
    "ASDEX I-mode", "asdex_i_mode", "ASDEX_I_mode_EM". Matching the raw string
    would make a natural query like "asdex i-mode" find nothing, so the query
    and the record text are both reduced to alphanumeric terms, and every term
    must appear.
    """
    return _ascii_words(query)


def _record_date(record: dict[str, Any]) -> str:
    """Best available date for range filtering, as an ISO-ish string."""
    for key in ("started_on", "created_at"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def search(
    query: str = "",
    *,
    machine: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    simulation_tag: str | None = None,
    sub_tag: str | None = None,
    origin: str | None = None,
    case: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    catalog_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Filter and free-text search the catalog. No LLM, no network.

    ``query`` matches every text field, including the human note and the
    imported notebook description. All terms must match, but separators do not
    have to: "asdex i-mode" finds "ASDEX_I_mode_EM". That is what makes "find
    the ASDEX I-mode run from two years ago" a one-liner.
    """
    terms = _query_terms(query)
    results: list[dict[str, Any]] = []
    for record in iter_records(catalog_dir=catalog_dir):
        record_simulation_tag, record_sub_tag = record_identity(record)
        if simulation_tag and slugify(simulation_tag) != record_simulation_tag:
            continue
        if sub_tag and str(sub_tag).strip() != record_sub_tag:
            continue
        if machine and machine not in (record.get("machines") or []):
            continue
        if status and record.get("status") != status:
            continue
        if origin and record.get("origin") != origin:
            continue
        if case and record.get("case") != case:
            continue
        if tag and tag not in (record.get("tags") or []):
            continue
        if after or before:
            date = _record_date(record)
            if after and (not date or date < after):
                continue
            if before and (not date or date > before):
                continue
        if terms:
            # Normalize the record the same way as the query, so separator
            # style never decides whether a simulation is findable.
            haystack = " ".join(_ascii_words(_haystack(record)))
            if not all(term in haystack for term in terms):
                continue
        results.append(summarize(record))
    if limit is not None and limit >= 0:
        results = results[:limit]
    return results


def show(record_id: str, *, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Full record plus its compact summary, for display."""
    record = read_record(record_id, catalog_dir=catalog_dir)
    return {"summary": summarize(record), "record": record}


def stats(*, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Counts by status, machine, and origin. Useful as a catalog health check."""
    by_status: dict[str, int] = {}
    by_machine: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    total = 0
    for record in iter_records(catalog_dir=catalog_dir):
        total += 1
        by_status[str(record.get("status"))] = by_status.get(str(record.get("status")), 0) + 1
        by_origin[str(record.get("origin"))] = by_origin.get(str(record.get("origin")), 0) + 1
        for machine in record.get("machines") or ["(unknown)"]:
            by_machine[str(machine)] = by_machine.get(str(machine), 0) + 1
    return {
        "total": total,
        "by_status": dict(sorted(by_status.items())),
        "by_machine": dict(sorted(by_machine.items())),
        "by_origin": dict(sorted(by_origin.items())),
        "catalog_dir": str(catalog_dir or CATALOG_DIR),
    }
