"""Import the historical simulation notebook into the catalog.

Before this toolchain existed, every simulation lived as a row in
``Other_data/simulation_note_XGC1.xlsx`` -- one sheet per year, one row per
numbered shot. Those runs are still the most valuable search corpus the group
has: they are what a researcher actually wants when asking "what did we do for
ASDEX I-mode?".

Importing them as catalog records means one search covers both the historical
work and everything the automation runs from now on. The import is deliberately
lossy in one direction only: it never invents provenance. Imported records carry
``origin: notebook``, keep the researcher's original wording verbatim under
``legacy``, and never claim a verified input, commit, or run.

The notebook itself is never written to. It stays the human's own document.

Two facts about the historical numbering shaped the code:

* Numbers are not globally unique before the ``n<NUMBER><MACHINE>`` scheme --
  ``ti204`` and ``su204`` are different simulations. A cleaned shot becomes the
  simulation tag, while the original shot/directory label becomes the sub-tag.
* Some shot cells are annotations rather than shots ("Numbering scheme has
  changed...", "n585fr (dup number)"). Those are reported as skipped with the
  reason, not silently dropped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from tools import case_promotion as cp
from tools import simulation_catalog as catalog

# Machine-code suffixes used by the modern shot names, so an imported record
# can state which machine it ran on without guessing.
_MACHINE_BY_CODE = {
    "pe": "perlmutter",
    "po": "polaris",
    "au": "aurora",
    "do": "doudna",
    "fr": "frontier",
    "su": "summit",
    "st": "stellar",
    "fl": "flux",
    "sa": "stellarai",
}

# Retired machines the researcher encoded as a name prefix before the current
# scheme existed. Each is listed only where the prefix and the sheet years agree
# exactly (ti* runs are 2013-2019, Titan ran 2012-2019, and so on). Prefixes
# whose meaning is not certain -- pf, hp, ew, eo, ed, cr, mi -- are deliberately
# absent: no machine is better than the wrong machine.
_LEGACY_MACHINE_BY_PREFIX = {
    "ti": "titan",
    "co": "cori",
    "th": "theta",
    "su": "summit",
    "po": "polaris",
    "pe": "perlmutter",
}

_MODERN_SHOT_RE = re.compile(r"^n(?P<number>[0-9]{2,4})(?P<code>[a-z]{2})$")
_LEGACY_SHOT_RE = re.compile(r"^(?P<prefix>[a-z]+)(?P<number>[0-9]+)$")

# Notebook rows whose "shot" cell is prose rather than an identifier.
_ANNOTATION_HINTS = ("numbering scheme", "empty")

# The notebook's "data" column names where the output ended up. The wording
# varies ("hpss", "HPSS nersc", "on tape"), so match on tokens rather than the
# whole cell.
_ARCHIVE_TOKENS = {
    "hpss": "hpss",
    "tape": "hpss",
}


class LegacyImportError(RuntimeError):
    """The notebook cannot be imported as requested."""


def _clean_shot(shot: str) -> tuple[str | None, str | None]:
    """Reduce a notebook shot cell to a catalog id, or explain the refusal."""
    raw = str(shot or "").strip()
    if not raw:
        return None, "empty shot cell"
    lowered = raw.lower()
    for hint in _ANNOTATION_HINTS:
        if hint in lowered:
            return None, f"annotation row, not a simulation ({raw!r})"

    # "n585fr (dup number)", "th337 (saved as th336)", "n531fr - pe":
    # take the leading identifier and leave the rest as evidence. The trailing
    # machine code is only absorbed when it is a code we recognize, so a shot
    # like "f107-f115" is not silently extended.
    match = re.match(r"^([A-Za-z]+[0-9]+)([A-Za-z]{2})?", raw)
    if not match:
        return None, f"no identifier found in shot cell {raw!r}"
    candidate = match.group(1).lower()
    suffix = (match.group(2) or "").lower()
    if suffix in _MACHINE_BY_CODE:
        candidate += suffix
    try:
        return catalog.validate_record_id(candidate), None
    except catalog.SimulationCatalogError as exc:
        return None, str(exc)


def _machines_for(record_id: str, note: cp.SimulationNote) -> tuple[list[str], str | None]:
    """Infer the machine from the shot name, and say how it was inferred.

    Returns the machine list plus the basis for it, which is recorded on the
    imported record. An inferred machine is weaker evidence than a machine the
    automation observed, and the record should not pretend otherwise.
    """
    modern = _MODERN_SHOT_RE.fullmatch(record_id)
    if modern:
        machine = _MACHINE_BY_CODE.get(modern.group("code"))
        if machine:
            return [machine], "shot name machine code"
    legacy = _LEGACY_SHOT_RE.fullmatch(record_id)
    if legacy:
        # Older shots such as "ti204" encode a now-retired machine as a prefix.
        machine = _LEGACY_MACHINE_BY_PREFIX.get(legacy.group("prefix"))
        if machine:
            return [machine], "legacy shot name prefix"

    # Some rows abbreviate the shot ("n555") but spell it out in full in the
    # description ("n555pe_ASDEX_I_mode_turb_ES"). Accept that only when the
    # number matches, so an unrelated mention cannot assign a machine.
    number = catalog.record_number(record_id)
    if number is not None:
        # The name is normally followed by an underscore ("n555pe_ASDEX..."),
        # which \b does not treat as a boundary, so spell the delimiters out.
        for candidate in re.findall(
            r"(?:^|[^a-z0-9])n([0-9]{2,4})([a-z]{2})(?![a-z0-9])",
            str(note.description or "").lower(),
        ):
            if int(candidate[0]) == number:
                machine = _MACHINE_BY_CODE.get(candidate[1])
                if machine:
                    return [machine], "full shot name in the description"
    return [], None


def _sheet_year(sheet: str) -> str | None:
    match = re.search(r"(20[0-9]{2})", str(sheet or ""))
    return match.group(1) if match else None


def _started_on(note: cp.SimulationNote) -> str:
    """A sortable date. The explicit cell when present, else the sheet's year."""
    raw = str(note.date or "").strip()
    if raw:
        # openpyxl hands back "2026-01-23 00:00:00" for real dates; the leading
        # ISO date is enough for range filtering and sorts correctly as text.
        match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if match:
            return match.group(1)
    year = _sheet_year(note.sheet)
    return f"{year}-01-01" if year else ""


def _archive_for(note: cp.SimulationNote) -> dict[str, Any]:
    words = set(re.findall(r"[a-z]+", str(note.data or "").lower()))
    system = next(
        (name for token, name in _ARCHIVE_TOKENS.items() if token in words), None
    )
    return {
        "system": system,
        # The notebook records that output went to tape, not where. A human or a
        # later HPSS tool fills the path; inventing one would be a lie.
        "path": None,
        "archived_at": None,
        "notebook_value": note.data or None,
    }


def _tags_for(note: cp.SimulationNote, outcome: str) -> list[str]:
    tags = [f"notebook-{outcome}"]
    year = _sheet_year(note.sheet)
    if year:
        tags.append(f"year-{year}")
    code = str(note.code or "").strip().lower()
    for token, tag in (
        ("xgca", "xgca"),
        ("em", "electromagnetic"),
        ("es", "electrostatic"),
        ("adiabatic", "adiabatic"),
        ("deltaf", "delta-f"),
        ("df", "delta-f"),
    ):
        if token in re.findall(r"[a-z]+", code) and tag not in tags:
            tags.append(tag)
    return tags


def _status_for(outcome: str, archive: dict[str, Any]) -> str:
    """Map a notebook outcome onto a catalog status.

    Historical runs are finished by definition, so the only question is how they
    ended. Anything the notebook does not clearly call a failure is recorded as
    completed, with the classification preserved in ``legacy.outcome`` so the
    distinction between "worked" and "we never said" is not lost.
    """
    if outcome == "failed":
        return "failed"
    if archive.get("system"):
        return "archived"
    return "completed"


def build_record(
    note: cp.SimulationNote,
    *,
    record_id: str | None = None,
    sub_tag: str | None = None,
) -> dict[str, Any] | None:
    """Turn one notebook row into a catalog record. Writes nothing.

    Returns None for rows that are not simulations; use ``plan_import`` when the
    reason for skipping matters.
    """
    simulation_tag, _reason = _clean_shot(note.shot)
    if simulation_tag is None:
        return None

    resolved_sub_tag = sub_tag or catalog.directory_sub_tag(
        note.shot.strip(), simulation_tag
    )
    resolved_record_id = record_id or simulation_tag

    classification = cp.classify_outcome(note.combined_text())
    outcome = classification["outcome"]
    archive = _archive_for(note)
    machines, machine_basis = _machines_for(simulation_tag, note)
    record = catalog.new_record(
        resolved_record_id,
        simulation_tag=simulation_tag,
        sub_tag=resolved_sub_tag,
        machines=machines,
        short_name=note.description or note.shot,
        directory_name=note.shot.strip(),
        tags=_tags_for(note, outcome),
        status=_status_for(outcome, archive),
        origin="notebook",
        note="",
    )
    record["archive"] = archive
    record["machine_basis"] = machine_basis
    record["started_on"] = _started_on(note)
    record["description"] = note.description
    record["code"] = note.code
    record["geometry"] = note.geometry
    # Everything the researcher actually wrote, kept verbatim and attributed, so
    # a surprising status can always be traced back to its source row.
    record["legacy"] = {
        "shot": note.shot,
        "sheet": note.sheet,
        "number": note.number,
        "description": note.description,
        "code": note.code,
        "geometry": note.geometry,
        "note": note.note,
        "date": note.date,
        "data": note.data,
        "outcome": outcome,
        "outcome_evidence": {
            "failure": classification["failure_evidence"],
            "success": classification["success_evidence"],
            "caution": classification["caution_evidence"],
        },
        "source": "Other_data/simulation_note_XGC1.xlsx",
    }
    return record


def plan_import(
    *,
    notes: Iterable[cp.SimulationNote] | None = None,
    notebook: Path | None = None,
    catalog_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Work out what the import would do. Writes nothing.

    Existing records are left alone unless ``overwrite`` is set, and records the
    automation created (``origin: automation``) are never overwritten at all --
    a notebook row must not clobber a machine-verified simulation.
    """
    try:
        source_notes = list(notes) if notes is not None else cp.load_simulation_notes(notebook)
    except cp.CasePromotionError as exc:
        raise LegacyImportError(str(exc)) from exc

    create: list[dict[str, Any]] = []
    update: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}
    reserved_ids: set[str] = set()
    existing_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for existing_record in catalog.iter_records(catalog_dir=catalog_dir):
        identity = catalog.record_identity(existing_record)
        if identity in existing_by_identity:
            first = existing_by_identity[identity].get("record_id")
            second = existing_record.get("record_id")
            raise LegacyImportError(
                f"catalog already contains duplicate simulation_tag + sub_tag "
                f"items: {first}, {second}"
            )
        existing_by_identity[identity] = existing_record

    for note in source_notes:
        simulation_tag, reason = _clean_shot(note.shot)
        if simulation_tag is None:
            skip.append({"shot": note.shot, "sheet": note.sheet, "reason": reason})
            continue
        sub_tag = catalog.directory_sub_tag(note.shot.strip(), simulation_tag)
        identity = catalog.identity_key(simulation_tag, sub_tag)
        if identity in seen:
            skip.append(
                {
                    "shot": note.shot,
                    "sheet": note.sheet,
                    "simulation_tag": simulation_tag,
                    "sub_tag": sub_tag,
                    "reason": (
                        f"duplicate simulation_tag + sub_tag of "
                        f"{seen[identity]!r} already imported"
                    ),
                }
            )
            continue

        existing = existing_by_identity.get(identity)
        record_id = (
            str(existing["record_id"])
            if existing is not None
            else catalog.identity_record_id(
                simulation_tag,
                sub_tag,
                catalog_dir=catalog_dir,
                reserved_ids=reserved_ids,
            )
        )
        if existing is not None:
            if existing.get("origin") != "notebook":
                skip.append(
                    {
                        "shot": note.shot,
                        "sheet": note.sheet,
                        "record_id": record_id,
                        "reason": (
                            f"existing record has origin "
                            f"{existing.get('origin')!r}; refusing to overwrite "
                            f"automation-owned provenance"
                        ),
                    }
                )
                continue
            if not overwrite:
                skip.append(
                    {
                        "shot": note.shot,
                        "sheet": note.sheet,
                        "record_id": record_id,
                        "reason": "already imported; pass overwrite=True to refresh",
                    }
                )
                continue

        record = build_record(note, record_id=record_id, sub_tag=sub_tag)
        if record is None:  # pragma: no cover - _clean_shot already filtered
            continue
        seen[identity] = note.shot
        reserved_ids.add(record_id)
        entry = {
            "record_id": record_id,
            "simulation_tag": simulation_tag,
            "sub_tag": sub_tag,
            "shot": note.shot,
            "sheet": note.sheet,
            "status": record["status"],
            "outcome": record["legacy"]["outcome"],
            "machines": record["machines"],
            "description": note.description,
            "record": record,
        }
        (update if existing is not None else create).append(entry)

    return {
        "notes_read": len(source_notes),
        "create": create,
        "update": update,
        "skip": skip,
        "counts": {
            "create": len(create),
            "update": len(update),
            "skip": len(skip),
        },
        "catalog_dir": str(catalog_dir or catalog.CATALOG_DIR),
        "writes": False,
    }


def import_notebook(
    *,
    notes: Iterable[cp.SimulationNote] | None = None,
    notebook: Path | None = None,
    catalog_dir: Path | None = None,
    overwrite: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Write the imported records after explicit confirmation.

    Without ``confirm`` this returns the plan and touches nothing, matching the
    repository's human-in-the-loop rule for anything that writes.
    """
    plan = plan_import(
        notes=notes,
        notebook=notebook,
        catalog_dir=catalog_dir,
        overwrite=overwrite,
    )
    if not confirm:
        plan["imported"] = False
        plan["reason"] = (
            "confirmation required; review the counts and resubmit with confirm=True"
        )
        return plan

    written: list[str] = []
    for entry in plan["create"] + plan["update"]:
        catalog.write_record(entry["record"], catalog_dir=catalog_dir)
        written.append(entry["record_id"])

    # The full records are only needed for review; drop them from the receipt.
    for entry in plan["create"] + plan["update"]:
        entry.pop("record", None)
    plan["imported"] = True
    plan["written"] = written
    plan["writes"] = True
    return plan
