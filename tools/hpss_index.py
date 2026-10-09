"""Index of what is on tape (HPSS) for a machine, joined to the catalog.

``hsi ls -lR`` on the machine lists the user's HPSS home. This module keeps a
parsed copy of that listing in the catalog repository,
``kb/archives/<machine>_hpss.yaml`` (``paths.ARCHIVES_DIR``), and matches
every archive file to simulation records by the number at the front of its
name (``n498pe_D3D_WPQH....tar`` -> ``n498pe``; ``co433_...tar`` -> ``co433``).
The historical notebook only says "data: hpss"; this index says where.

The index is a record of the tape, not a plan: it never moves data. ``link``
copies a matched archive's path into a record's ``archive`` field when the
record has none and exactly one archive matches it; status is left alone,
because ``archived`` stays a human call (``docs/simulation-catalog.md``).

Rules for the transfer itself: ``docs/archive-perlmutter.md``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from tools import machines, paths, ssh
from tools import simulation_catalog as catalog

LISTING_COMMAND = ["hsi", "-q", "ls", "-lR"]
SYSTEM = "hpss"

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
)}
# -rw-r-----    1 sku       sku      10557577216 Jan 24  2024 CBC_df.tar
# drwxr-sr-x    4 sku       sku             9216 Mar  9 14:02 OLCF
_ENTRY_RE = re.compile(
    r"^(?P<mode>[-dl][rwxsStT-]{9})\s+\d+\s+(?P<owner>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<yt>\d{4}|\d{1,2}:\d{2})\s+(?P<name>.+?)\s*$"
)
_DIR_HEADER_RE = re.compile(r"^(?P<path>/\S.*):\s*$")
# The simulation tag at the front of an archive name: n498pe, n540, d0003po,
# co433, hp152, fus022 ... followed by a separator or the end.
_SUFFIXES = "pe|po|au|sa|fl|su|fr"  # machine codes used after the number (summit, frontier included)
_TAG_RE = re.compile(rf"^(?P<tag>[a-z]{{1,4}}\d{{2,4}}(?:{_SUFFIXES})?)(?=[_.\-]|$)", re.IGNORECASE)
_SUFFIX_RE = re.compile(rf"({_SUFFIXES})$")
_DIR_HEADER_ML = re.compile(r"^(?P<path>/\S.*):\s*$", re.MULTILINE)


class HPSSIndexError(RuntimeError):
    pass


def index_path(machine: str, *, archives_dir: Path | None = None) -> Path:
    return (archives_dir or paths.ARCHIVES_DIR) / f"{machine}_{SYSTEM}.yaml"


def _parse_date(mon: str, day: str, year_or_time: str, now: datetime) -> str:
    month = _MONTHS.get(mon)
    if month is None:
        return ""
    if ":" in year_or_time:
        # Within the last year: hsi prints the time instead of the year.
        year = now.year
        candidate = datetime(year, month, int(day))
        if candidate > now:
            year -= 1
        return f"{year:04d}-{month:02d}-{int(day):02d}"
    return f"{int(year_or_time):04d}-{month:02d}-{int(day):02d}"


def parse_listing(text: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Turn ``hsi ls -lR`` output into flat file entries.

    Files inside a ``.bp`` directory (ADIOS2 subfiles ``data.N``, ``md.0``) are
    folded into one entry for the directory with the summed size, so a
    checkpoint kept on tape as a directory shows as one thing.
    """
    now = now or datetime.now()
    entries: list[dict[str, Any]] = []
    folded: dict[str, dict[str, Any]] = {}
    current = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        header = _DIR_HEADER_RE.match(line)
        if header:
            current = header.group("path")
            continue
        m = _ENTRY_RE.match(line)
        if not m or not current:
            continue
        name = m.group("name")
        mode = m.group("mode")
        if mode.startswith("d"):
            continue  # directories appear as headers; their files follow
        size = int(m.group("size"))
        modified = _parse_date(m.group("mon"), m.group("day"), m.group("yt"), now)
        bp_dir = next((part for part in current.split("/") if part.endswith(".bp")), None)
        if bp_dir:
            head = current[: current.index(bp_dir) + len(bp_dir)]
            agg = folded.setdefault(head, {
                "path": head, "name": Path(head).name, "kind": "directory",
                "size": 0, "files": 0, "modified": modified,
            })
            agg["size"] += size
            agg["files"] += 1
            agg["modified"] = max(agg["modified"], modified)
            continue
        entries.append({
            "path": f"{current.rstrip('/')}/{name}", "name": name, "kind": "file",
            "size": size, "modified": modified,
        })
    entries.extend(folded.values())
    # htar writes <archive>.idx beside the archive; attach it instead of listing it.
    by_path = {e["path"]: e for e in entries}
    for e in list(entries):
        if e["name"].endswith(".idx") and e["path"][:-4] in by_path:
            target = by_path[e["path"][:-4]]
            target["htar_index"] = True
            target["index_size"] = e["size"]
            entries.remove(e)
    entries.sort(key=lambda e: e["path"])
    return entries


def fetch_listing(machine: str, *, timeout: float = 900.0) -> str:
    """Run ``hsi ls -lR`` on the machine. hsi prints the listing on stderr."""
    cfg = machines.get_machine(machine)
    if not cfg.get("hpss", True):
        raise HPSSIndexError(f"{machine} has no HPSS (machines.yaml hpss: false)")
    target = ssh.host_for(cfg)
    result = ssh.run(target, LISTING_COMMAND, timeout=timeout)
    text = result.stdout + result.stderr
    if result.returncode != 0 or not _DIR_HEADER_ML.search(text):
        raise HPSSIndexError(
            f"hsi listing failed on {machine} (rc {result.returncode}): {text[-500:]}"
        )
    return text


def is_archive(entry: dict[str, Any]) -> bool:
    """Anything on tape that is not an htar index: tar, tgz, cf, a bare directory copy."""
    return not entry["name"].endswith(".idx")


def _record_tags(records: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Every tag a record answers to, lower-cased: record id, simulation id/tag, directory name."""
    tags: dict[str, list[str]] = {}
    for record in records:
        record_id = str(record.get("record_id"))
        names = {record_id, record.get("simulation_id"), record.get("simulation_tag")}
        directory = record.get("directory_name")
        if directory:
            m = _TAG_RE.match(str(directory))
            if m:
                names.add(m.group("tag"))
        for name in names:
            if name:
                tags.setdefault(str(name).lower(), [])
                if record_id not in tags[str(name).lower()]:
                    tags[str(name).lower()].append(record_id)
    return tags


def match_records(
    entries: list[dict[str, Any]], records: Iterable[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Attach ``records`` (catalog ids) to every entry whose name starts with a known tag.

    A bare number (``n540_...``) matches the records of every machine suffix
    (``n540pe``, ``n540po``); a suffixed one (``n498po_...``) matches only the
    record with that suffix, so a Polaris copy never lands on the Perlmutter
    record.
    """
    records = list(records) if records is not None else list(catalog.iter_records())
    tags = _record_tags(records)
    numeric = {}
    for tag, ids in tags.items():
        stem = _SUFFIX_RE.sub("", tag)
        numeric.setdefault(stem, [])
        for rid in ids:
            if rid not in numeric[stem]:
                numeric[stem].append(rid)
    for entry in entries:
        m = _TAG_RE.match(entry["name"])
        if not m:
            entry["records"] = []
            continue
        tag = m.group("tag").lower()
        if _SUFFIX_RE.search(tag):
            # A machine suffix is a claim: n498po is not n498pe. Exact or nothing.
            matched = list(tags.get(tag) or [])
        else:
            matched = list(tags.get(tag) or numeric.get(tag) or [])
        entry["records"] = matched
    return entries


def build_index(machine: str, text: str, *, root: str | None = None) -> dict[str, Any]:
    entries = match_records(parse_listing(text))
    archives = [e for e in entries if is_archive(e)]
    first = _DIR_HEADER_ML.search(text)
    return {
        "machine": machine,
        "system": SYSTEM,
        "root": root or (first.group("path") if first else None),
        "command": " ".join(LISTING_COMMAND),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "entries": len(entries),
            "archives": len(archives),
            "bytes": sum(e["size"] for e in entries),
            "matched": sum(1 for e in entries if e.get("records")),
            "unmatched": sum(1 for e in entries if not e.get("records")),
        },
        "entries": entries,
    }


def write_index(index: dict[str, Any], *, archives_dir: Path | None = None) -> Path:
    path = index_path(index["machine"], archives_dir=archives_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(index, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
    return path


def load_index(machine: str, *, archives_dir: Path | None = None) -> dict[str, Any]:
    path = index_path(machine, archives_dir=archives_dir)
    if not path.is_file():
        raise HPSSIndexError(f"no HPSS index for {machine}: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HPSSIndexError(f"unreadable HPSS index: {path}")
    return data


def refresh(machine: str, *, text: str | None = None, archives_dir: Path | None = None) -> dict[str, Any]:
    """Fetch (or take) a listing, parse, match, write ``kb/archives/<machine>_hpss.yaml``."""
    text = text if text is not None else fetch_listing(machine)
    index = build_index(machine, text)
    path = write_index(index, archives_dir=archives_dir)
    return {"written": True, "path": str(path), "totals": index["totals"]}


def link_plan(index: dict[str, Any], records: Iterable[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Records that have no ``archive.path`` and exactly one archive on tape."""
    records = list(records) if records is not None else list(catalog.iter_records())
    by_id = {str(r.get("record_id")): r for r in records}
    candidates: dict[str, list[dict[str, Any]]] = {}
    for entry in index.get("entries", []):
        if not is_archive(entry):
            continue
        for rid in entry.get("records", []):
            candidates.setdefault(rid, []).append(entry)
    plan = []
    for rid, hits in sorted(candidates.items()):
        record = by_id.get(rid)
        if record is None:
            continue
        archive = record.get("archive") or {}
        if archive.get("path"):
            continue
        plan.append({
            "record_id": rid,
            "status": record.get("status"),
            "archives": [h["path"] for h in hits],
            "action": "link" if len(hits) == 1 else "ambiguous",
            "path": hits[0]["path"] if len(hits) == 1 else None,
            "archived_at": hits[0]["modified"] if len(hits) == 1 else None,
        })
    return plan


def apply_links(plan: list[dict[str, Any]], *, catalog_dir: Path | None = None) -> list[str]:
    linked = []
    for item in plan:
        if item["action"] != "link":
            continue
        record = catalog.read_record(item["record_id"], catalog_dir=catalog_dir)
        archive = dict(record.get("archive") or {})
        archive.update({"system": SYSTEM, "path": item["path"], "archived_at": item["archived_at"]})
        catalog.update_record(item["record_id"], {"archive": archive}, catalog_dir=catalog_dir)
        linked.append(item["record_id"])
    return linked


def entries_for(record_id: str, *, archives_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every tape entry, on every indexed machine, matched to this record."""
    directory = archives_dir or paths.ARCHIVES_DIR
    found: list[dict[str, Any]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob(f"*_{SYSTEM}.yaml")):
        try:
            index = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(index, dict):
            continue
        for entry in index.get("entries", []):
            if record_id in entry.get("records", []):
                found.append({**entry, "machine": index.get("machine"), "system": index.get("system")})
    return found


def summarize(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for e in index.get("entries", []):
        rows.append({
            "path": e["path"], "size": e["size"], "modified": e["modified"],
            "records": ",".join(e.get("records", [])) or "-",
        })
    return rows


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} PB"
