"""Read-only inventory of historical XGC input backups.

The archive contains inputs spanning many XGC schemas. Recency is used only as
a prioritization signal; no archived input becomes a validated baseline until
it is migrated, paired with its referenced files, and accepted by the current
``xgc --dryrun``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "input_backup"
CACHE_DIR = PROJECT_ROOT / ".cache"
_NUMBERED_NAME_RE = re.compile(
    r"^n(?P<number>[0-9]{3,})(?P<codes>[a-z]{2}(?:_[a-z]{2})*)?(?:_|$)",
    re.IGNORECASE,
)
_NAMELIST_RE = re.compile(r"^\s*&([A-Za-z][A-Za-z0-9_]*)")
_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*)(?:\s*\([^)]*\))?\s*=\s*(.*?)\s*$"
)

# Suffixes marking a file as a variant of a primary input rather than the
# input itself: alternates (.turb/.neo), fragments (.inc), packaging (.tar),
# and editor/date backups (.bak1, .Jul10, .1stday).
_VARIANT_SUFFIX_RE = re.compile(
    r"\.(?:tar|inc|neo|turb(?:\.[A-Za-z0-9_.]+)?|nocol|bak\d*|orig|old"
    r"|[A-Z][a-z]{2}\d{1,2}|1stday)$"
)

KEY_PARAMETERS = (
    "sml_dt",
    "sml_mstep",
    "sml_restart",
    "sml_bounce",
    "sml_inpsi",
    "sml_outpsi",
    "sml_nphi_total",
    "sml_wedge_n",
    "sml_em",
    "sml_electromagnetic",
    "col_mode",
    "ptl_marker_type",
    "sml_deltaf",
    "sml_deltaf_elec",
    "sml_3db_on",
    "ptb_3db_on",
)


class InputArchiveError(RuntimeError):
    """The archive cannot be inspected safely."""


def _strip_comment(line: str) -> str:
    """Remove a Fortran ``!`` comment while respecting quoted strings."""
    quote: str | None = None
    out: list[str] = []
    for char in line:
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        if char == "!" and quote is None:
            break
        out.append(char)
    return "".join(out)


def parse_input_text(text: str) -> dict[str, Any]:
    """Extract namelist names and one-line assignments without rewriting."""
    namelists: list[str] = []
    parameters: dict[str, str] = {}
    duplicate_parameters: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        namelist = _NAMELIST_RE.match(line)
        if namelist:
            name = namelist.group(1).lower()
            if name not in namelists:
                namelists.append(name)
            continue
        assignment = _ASSIGNMENT_RE.match(line)
        if not assignment:
            continue
        name = assignment.group(1).lower()
        if name in parameters and name not in duplicate_parameters:
            duplicate_parameters.append(name)
        parameters[name] = assignment.group(2).strip()
    return {
        "namelists": namelists,
        "parameters": parameters,
        "duplicate_parameters": duplicate_parameters,
    }


def _metadata_parameter_names(metadata_path: Path | None) -> tuple[set[str], str | None]:
    path = metadata_path
    if path is None:
        candidates = sorted(CACHE_DIR.glob("metadata-*.json"))
        path = candidates[-1] if candidates else None
    if path is None or not path.is_file():
        return set(), None
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputArchiveError(f"cannot read XGC metadata cache {path}: {exc}") from exc
    names = {
        str(name).lower()
        for namelist in metadata.values()
        if isinstance(namelist, dict)
        for name in (namelist.get("parameters") or {})
    }
    return names, str(path)


def _filename_tags(name: str) -> list[str]:
    known = {
        "iter": "iter",
        "st40": "st40",
        "kstar": "kstar",
        "d3d": "d3d",
        "diiid": "d3d",
        "tcv": "tcv",
        "asdex": "asdex-u",
        "cbc": "cbc",
        "ecbc": "ecbc",
        "xgca": "xgca",
        "xgc1": "xgc1",
        "em": "electromagnetic",
        "es": "electrostatic",
        "heatload": "heat-load",
        "rmp": "rmp",
    }
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9]+", name)}
    return sorted({tag for word, tag in known.items() if word in words})


def _machine_names(codes: list[str]) -> list[str]:
    code_to_machine = {
        "pe": "perlmutter",
        "po": "polaris",
        "au": "aurora",
        "do": "doudna",
        "fr": "frontier",
        "su": "summit",
        "st": "stellar",
        "fl": "flux",
    }
    return [code_to_machine.get(code, f"unknown:{code}") for code in codes]


def _variant_kind(name: str) -> str | None:
    """The variant suffix if this is not a primary input, else None.

    Variants are alternates (`input.neo`, `input.turb`), fragments (`.inc`),
    packaging (`.tar`), or backups (`.bak1`, `.Jul10`). They document how a
    primary input was used but should not be ranked as independent runs.
    """
    match = _VARIANT_SUFFIX_RE.search(name)
    return match.group(0).lstrip(".") if match else None


def _inspect_file(
    path: Path,
    match: re.Match[str],
    current_parameters: set[str],
) -> dict[str, Any]:
    raw = path.read_bytes()
    is_tar = path.name.endswith(".tar")
    if is_tar:
        # tar payloads are binary; decoding them as text would yield garbage
        # "parameters" and a meaningless schema comparison.
        text = ""
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    parsed = parse_input_text(text)
    parameters = parsed["parameters"]
    names = set(parameters)
    unknown = sorted(names - current_parameters) if current_parameters else []
    recognized = sorted(names & current_parameters) if current_parameters else []
    ratio = (
        round(len(recognized) / len(names), 4)
        if current_parameters and names
        else None
    )
    if ratio is None:
        alignment = "not_compared"
    elif ratio >= 0.95:
        alignment = "high"
    elif ratio >= 0.80:
        alignment = "partial"
    else:
        alignment = "low"

    codes_text = (match.group("codes") or "").lower()
    codes = codes_text.split("_") if codes_text else []
    # For files inside a numbered directory, the directory carries the
    # simulation identity: report `<dir>/<file>` and treat the plain `input`
    # as the primary, everything else as a variant.
    in_numbered_dir = (
        path.parent != ARCHIVE_DIR
        and _NUMBERED_NAME_RE.match(path.parent.name) is not None
    )
    if in_numbered_dir:
        display_name = f"{path.parent.name}/{path.name}"
        variant = None if path.name == "input" else _variant_kind(path.name)
    else:
        display_name = path.name
        variant = _variant_kind(path.name)
    return {
        "simulation_number": int(match.group("number")),
        "archive_name": display_name,
        "variant": variant,
        "relative_path": str(path.relative_to(ARCHIVE_DIR)),
        "machine_codes": codes,
        "machines": _machine_names(codes),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "namelists": parsed["namelists"],
        "parameter_count": len(names),
        "duplicate_parameters": parsed["duplicate_parameters"],
        "schema_comparison": {
            "alignment": alignment,
            "recognized_count": len(recognized) if current_parameters else None,
            "unknown_count": len(unknown) if current_parameters else None,
            "recognized_ratio": ratio,
            "unknown_parameters": unknown,
        },
        "key_parameters": {
            name: parameters[name] for name in KEY_PARAMETERS if name in parameters
        },
        "filename_tags": _filename_tags(path.name),
        "validation_status": "unvalidated_archive",
    }


def inspect_archive(
    *,
    min_number: int = 600,
    max_files: int = 100,
    metadata_path: Path | None = None,
) -> dict[str, Any]:
    """Inspect recent numbered inputs without modifying or validating them."""
    if min_number < 0:
        raise InputArchiveError("min_number must be non-negative")
    if max_files < 1 or max_files > 2_000:
        raise InputArchiveError("max_files must be between 1 and 2000")
    if not ARCHIVE_DIR.is_dir():
        raise InputArchiveError(f"input archive not found: {ARCHIVE_DIR}")

    current_parameters, metadata_source = _metadata_parameter_names(metadata_path)
    all_files = sorted(path for path in ARCHIVE_DIR.rglob("*") if path.is_file())
    numbered: list[tuple[Path, re.Match[str]]] = []
    for path in all_files:
        match = _NUMBERED_NAME_RE.match(path.name)
        if match:
            numbered.append((path, match))
            continue
        # Some runs are archived as a numbered *directory* holding `input`
        # plus variants (e.g. n523po_CBC_totalf_lin2/input.neo). The directory
        # name carries the simulation number in that layout.
        if path.parent != ARCHIVE_DIR and path.name.startswith("input"):
            parent_match = _NUMBERED_NAME_RE.match(path.parent.name)
            if parent_match:
                numbered.append((path, parent_match))

    selected = [
        (path, match)
        for path, match in numbered
        if int(match.group("number")) >= min_number
    ]
    selected.sort(
        key=lambda item: (int(item[1].group("number")), item[0].name),
        reverse=True,
    )
    truncated = len(selected) > max_files
    selected = selected[:max_files]
    records = [
        _inspect_file(path, match, current_parameters) for path, match in selected
    ]

    by_number: dict[int, list[str]] = {}
    for record in records:
        by_number.setdefault(record["simulation_number"], []).append(
            record["archive_name"]
        )
    duplicates = {
        str(number): names for number, names in by_number.items() if len(names) > 1
    }
    alignments: dict[str, int] = {}
    for record in records:
        value = record["schema_comparison"]["alignment"]
        alignments[value] = alignments.get(value, 0) + 1

    return {
        "schema": "xgc-input-archive-inventory/v1",
        "archive_root": str(ARCHIVE_DIR),
        "selection": {
            "min_number": min_number,
            "max_files": max_files,
            "selected_before_limit": len(
                [
                    1
                    for _, match in numbered
                    if int(match.group("number")) >= min_number
                ]
            ),
            "returned": len(records),
            "truncated": truncated,
        },
        "counts": {
            "all_files": len(all_files),
            "numbered_files": len(numbered),
            "distinct_selected_numbers": len(by_number),
            "schema_alignment": alignments,
        },
        "metadata_source": metadata_source,
        "duplicate_number_variants": duplicates,
        "records": records,
        "warnings": [
            "Archive inputs are read-only evidence, not validated baselines.",
            "Filename recency does not prove runtime compatibility.",
            "Each candidate still needs referenced input files and current xgc --dryrun.",
        ],
    }
