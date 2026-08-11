"""Local provenance layout shared by submission, analysis, and GUI tools."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

PROVENANCE_CLASSES = ("master", "test")


class ProvenanceError(ValueError):
    """A provenance class or local record layout is invalid."""


def classify(
    requested: str | None,
    *,
    simulation_id: str | None,
    hello_world: bool,
) -> str:
    """Select the storage class for a new submission record.

    Registered scientific simulations are master records. Scheduler smoke tests
    and unregistered development runs default to test. Historical or otherwise
    unregistered scientific work may opt into master explicitly.
    """
    value = (
        requested
        if requested is not None
        else ("master" if simulation_id else "test")
    )
    if value not in PROVENANCE_CLASSES:
        allowed = ", ".join(PROVENANCE_CLASSES)
        raise ProvenanceError(
            f"provenance_class must be one of {allowed}; got {value!r}"
        )
    if simulation_id and value != "master":
        raise ProvenanceError("a registered simulation must use master provenance")
    if hello_world and value != "test":
        raise ProvenanceError("a hello-world job must use test provenance")
    return value


def record_dir(runs_dir: Path, run_id: str, provenance_class: str) -> Path:
    if provenance_class not in PROVENANCE_CLASSES:
        raise ProvenanceError(f"invalid provenance_class {provenance_class!r}")
    return runs_dir / provenance_class / run_id


def find_record(runs_dir: Path, run_id: str) -> tuple[Path, Path] | None:
    """Find a master, test, or legacy record and reject ambiguous run IDs."""
    candidates = [
        runs_dir / provenance_class / run_id / "provenance.json"
        for provenance_class in PROVENANCE_CLASSES
    ]
    candidates.append(runs_dir / run_id / "provenance.json")
    matches = [path for path in candidates if path.is_file()]
    if len(matches) > 1:
        locations = ", ".join(str(path) for path in matches)
        raise ProvenanceError(
            f"ambiguous provenance for run {run_id!r}: {locations}"
        )
    if not matches:
        return None
    return matches[0].parent, matches[0]


def iter_records(runs_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield classified records plus legacy top-level records."""
    for provenance_class in PROVENANCE_CLASSES:
        root = runs_dir / provenance_class
        if root.is_dir():
            for path in sorted(root.glob("*/provenance.json")):
                if path.is_file():
                    yield provenance_class, path
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*/provenance.json")):
            if path.is_file():
                yield "legacy", path
