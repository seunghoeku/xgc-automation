"""Index of experimental shot data: what each file is, what its columns mean,
and which simulations used it.

Profile files, equilibria, meshes and diagnostic exports arrive by e-mail,
from papers and from MDSplus, get renamed, and a year later nobody remembers
whether ``ti_tcv69271_t1.1_NT.prf`` came from CXRS system 1 or 3. The index
remembers. Two homes, as for everything else in the catalog:

* the **files** live in the data directory, ``<data_dir>/shots/<device>/<shot>/``
  (``paths.DATA_ROOT``), never in git;
* the **index** is one YAML per shot in the catalog repository,
  ``kb/shots/<device>_<shot>.yaml`` (``paths.SHOTS_DIR``): per file its path
  relative to the data directory, sha256 and size, kind, diagnostic, quantity,
  the meaning and unit of every column, where it came from, and ``used_by``,
  the simulations whose ``referenced_inputs`` carry the same sha256.

``used_by`` is derived, not typed: :func:`link` recomputes it from the
catalog records (a submission records the sha256 of every file the input
references), so the join between an experiment file and a run never depends
on a filename staying the same.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from tools import paths
from tools import catalog_sync
from tools import simulation_catalog as catalog

SCHEMA_VERSION = 1
KINDS = ("profile", "equilibrium", "mesh", "diagnostic", "other")


class ShotIndexError(ValueError):
    pass


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    if not slug:
        raise ShotIndexError(f"cannot make a name from {text!r}")
    return slug


def shot_key(device: str, shot: int | str) -> str:
    return f"{_slug(device)}_{str(shot).strip()}"


def index_path(device: str, shot: int | str, *, shots_dir: Path | None = None) -> Path:
    return (shots_dir or paths.SHOTS_DIR) / f"{shot_key(device, shot)}.yaml"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(device: str, shot: int | str, *, shots_dir: Path | None = None) -> dict[str, Any]:
    path = index_path(device, shot, shots_dir=shots_dir)
    if not path.is_file():
        raise ShotIndexError(f"no index for {device} #{shot} ({path})")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ShotIndexError(f"{path} is not a mapping")
    return data


def iter_shots(*, shots_dir: Path | None = None) -> Iterator[dict[str, Any]]:
    directory = shots_dir or paths.SHOTS_DIR
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and data.get("shot") is not None:
            yield data


def _write(data: dict[str, Any], *, shots_dir: Path | None = None) -> Path:
    path = index_path(data["device"], data["shot"], shots_dir=shots_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
    return path


def _new_index(device: str, shot: int | str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "device": _slug(device),
        "shot": int(shot) if str(shot).isdigit() else str(shot),
        "description": "",
        "time_ms": None,
        "files": [],
        "note": "",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@catalog_sync.synced(lambda result, **k: f"shot {k.get('device', '')} {k.get('shot', '')}: describe")
def describe(
    device: str,
    shot: int | str,
    *,
    description: str | None = None,
    time_ms: float | None = None,
    note: str | None = None,
    shots_dir: Path | None = None,
) -> dict[str, Any]:
    """Set the shot-level fields; creates the index if needed."""
    try:
        data = load(device, shot, shots_dir=shots_dir)
    except ShotIndexError:
        data = _new_index(device, shot)
    if description is not None:
        data["description"] = description
    if time_ms is not None:
        data["time_ms"] = time_ms
    if note is not None:
        data["note"] = note
    _write(data, shots_dir=shots_dir)
    return data


@catalog_sync.synced(lambda result, **k: f"shot {k.get('device', '')} {k.get('shot', '')}: {Path(str(k.get('source', ''))).name}")
def register_file(
    device: str,
    shot: int | str,
    source: str | Path,
    *,
    kind: str = "other",
    diagnostic: str = "",
    quantity: str = "",
    columns: dict[str, str] | None = None,
    origin: str = "",
    note: str = "",
    copy: bool = True,
    shots_dir: Path | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    """Add one file to a shot's index, copying it into the data directory.

    ``columns`` maps a column name to its meaning and unit, e.g.
    ``{"psi_n": "normalised poloidal flux", "ti": "ion temperature [eV], CXRS system 3"}``.
    ``origin`` says where the file came from (e-mail, paper, MDSplus tree).
    With ``copy=False`` the file is indexed where it is (absolute path).
    """
    if kind not in KINDS:
        raise ShotIndexError(f"kind must be one of {KINDS}, not {kind!r}")
    src = Path(source).expanduser()
    if not src.is_file():
        raise ShotIndexError(f"{src} is not a file")
    data_dir = (data_root or paths.DATA_ROOT) / "shots" / _slug(device) / str(shot)
    digest = sha256_of(src)

    if copy:
        data_dir.mkdir(parents=True, exist_ok=True)
        dest = data_dir / src.name
        if dest.exists() and sha256_of(dest) != digest:
            raise ShotIndexError(
                f"{dest} exists with different content; rename the new file or remove the old one"
            )
        if not dest.exists():
            shutil.copy2(src, dest)
        stored = str(dest.relative_to(data_root or paths.DATA_ROOT))
    else:
        stored = str(src.resolve())

    try:
        data = load(device, shot, shots_dir=shots_dir)
    except ShotIndexError:
        data = _new_index(device, shot)

    entry = {
        "name": src.name,
        "path": stored,
        "sha256": digest,
        "size_bytes": src.stat().st_size,
        "kind": kind,
        "diagnostic": diagnostic,
        "quantity": quantity,
        "columns": dict(columns or {}),
        "origin": origin,
        "note": note,
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "used_by": [],
    }
    files = [f for f in data.get("files", []) if f.get("sha256") != digest]
    previous = next((f for f in data.get("files", []) if f.get("sha256") == digest), None)
    if previous:
        # Re-registering the same content keeps what was already known.
        for key in ("columns", "diagnostic", "quantity", "origin", "note", "used_by", "kind"):
            if not entry.get(key) and previous.get(key):
                entry[key] = previous[key]
    files.append(entry)
    data["files"] = files
    _write(data, shots_dir=shots_dir)
    return entry


def _record_hashes(record: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    files = ((record.get("referenced_inputs") or {}).get("files") or {})
    for item in files.values():
        if isinstance(item, dict) and item.get("sha256"):
            hashes.add(str(item["sha256"]))
    for run in record.get("runs") or []:
        files = ((run.get("referenced_inputs") or {}).get("files") or {}) if isinstance(run, dict) else {}
        for item in files.values():
            if isinstance(item, dict) and item.get("sha256"):
                hashes.add(str(item["sha256"]))
    return hashes


def link(
    *,
    shots_dir: Path | None = None,
    catalog_dir: Path | None = None,
    records: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recompute ``used_by`` on every indexed file from the catalog's input hashes."""
    by_hash: dict[str, set[str]] = {}
    source = records if records is not None else catalog.iter_records(catalog_dir=catalog_dir)
    for record in source:
        rid = str(record.get("record_id") or record.get("simulation_id") or "")
        for digest in _record_hashes(record):
            by_hash.setdefault(digest, set()).add(rid)

    linked: dict[str, list[str]] = {}
    changed = 0
    for data in list(iter_shots(shots_dir=shots_dir)):
        touched = False
        for entry in data.get("files", []):
            users = sorted(by_hash.get(str(entry.get("sha256")), set()))
            if users != list(entry.get("used_by") or []):
                entry["used_by"] = users
                touched = True
            if users:
                linked[f"{shot_key(data['device'], data['shot'])}/{entry['name']}"] = users
        if touched:
            _write(data, shots_dir=shots_dir)
            changed += 1
    return {"shots_updated": changed, "linked": linked}


def lookup(sha256: str, *, shots_dir: Path | None = None) -> list[dict[str, Any]]:
    """Which indexed files have this hash (device, shot, entry)."""
    hits = []
    for data in iter_shots(shots_dir=shots_dir):
        for entry in data.get("files", []):
            if entry.get("sha256") == sha256:
                hits.append({"device": data["device"], "shot": data["shot"], **entry})
    return hits


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "device": data.get("device"),
        "shot": data.get("shot"),
        "time_ms": data.get("time_ms"),
        "description": data.get("description", ""),
        "files": len(data.get("files", [])),
        "used_by": sorted({u for f in data.get("files", []) for u in f.get("used_by", [])}),
    }
