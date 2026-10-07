"""Mirror the simulation catalog to a CSV notebook in the repository.

``Other_data/simulation_note_XGC1.xlsx`` was the researcher's hand-kept record
of every numbered run. It stays as the historical notebook (rows through
n656po), but it is a poor target for automation: openpyxl rewrites drop the
cached values of old formulas, and a binary workbook shows no diff in git.

``kb/simulation_notebook.csv`` replaces it going forward. It is the same view
-- one row per simulation, the agent's ``ai_note`` and the human ``note``
columns last -- but plain text and regenerated from the catalog after every
submission. It reuses ``tools.sheet_sync`` unchanged: a CSV file is just
another worksheet.

    catalog (kb/simulations/*.yaml)  --push-->  kb/simulation_notebook.csv
    catalog                          <--pull--  note column only

The CSV is a **derived file and is not tracked by git**: the catalog records
are the source of truth and travel through git, the CSV is rebuilt from them
on any checkout with ``xgc-sim sync-notebook --confirm`` (and by every
submission and ``set_ai_note``). Committing it would make every submission on
one machine conflict with every submission on another, for a file nobody
edits by hand except one column.

Edit the ``note`` cell of a row in any spreadsheet program, save as CSV, and
the next sync (or the next submission) pulls it into the catalog record. Every
other column is overwritten from the catalog; a hand-typed value there cannot
be verified and does not survive. A pull is only believed when the CSV is
newer than the record file: a copy left behind from before a ``git pull``
(or an older checkout on another machine) would otherwise overwrite a note
written since with ``xgc-sim note``. Such rows are reported as
``stale_notes`` and the catalog value is pushed over them.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from tools import sheet_sync

from tools import paths

PROJECT_ROOT = paths.PROJECT_ROOT
NOTEBOOK_PATH = paths.NOTEBOOK_PATH  # follows XGC_CATALOG_DIR (tools/paths.py)


class CsvWorksheet:
    """A CSV file behind the slice of the gspread API that sheet_sync uses."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def get_all_values(self) -> list[list[str]]:
        if not self.path.is_file():
            return []
        with self.path.open(newline="", encoding="utf-8") as handle:
            return [list(row) for row in csv.reader(handle)]

    def resize(self, rows: int, cols: int) -> None:
        # A file has no fixed shape; update() writes exactly the rows it gets.
        return None

    def update(self, values: list[list[Any]], range_name: str) -> None:
        if range_name != "A1":
            raise sheet_sync.SheetSyncError(
                f"CSV notebook only supports whole-table writes at A1, not {range_name}"
            )
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        for row in values:
            writer.writerow(["" if cell is None else str(cell) for cell in row])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a crash mid-write cannot leave a truncated file.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(buffer.getvalue(), encoding="utf-8")
        tmp.replace(self.path)


def _newer_than_record(csv_path: Path, catalog_dir: Path | None):
    """A pull filter: believe the CSV's note only if the CSV is newer than the record.

    git checkout gives a freshly pulled record a new mtime, so a CSV written
    before the pull is correctly treated as stale for that row.
    """
    from tools import simulation_catalog as catalog

    try:
        csv_mtime = csv_path.stat().st_mtime
    except OSError:
        return lambda record_id: False

    def newer(record_id: str) -> bool:
        try:
            record_mtime = catalog.record_path(record_id, catalog_dir=catalog_dir).stat().st_mtime
        except (OSError, ValueError):
            return True  # no record file to compare with; keep the old behaviour
        return csv_mtime > record_mtime

    return newer


def plan(*, path: Path | None = None, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Diff the catalog against the CSV. Writes nothing."""
    target = Path(path or NOTEBOOK_PATH)
    return sheet_sync.plan_sync(
        worksheet=CsvWorksheet(target),
        catalog_dir=catalog_dir,
        pull_filter=_newer_than_record(target, catalog_dir),
    )


def sync(
    *,
    path: Path | None = None,
    catalog_dir: Path | None = None,
    pull_notes: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Pull edited notes from the CSV, then rewrite it from the catalog."""
    target = Path(path or NOTEBOOK_PATH)
    payload = sheet_sync.sync(
        worksheet=CsvWorksheet(target),
        catalog_dir=catalog_dir,
        pull_notes=pull_notes,
        confirm=confirm,
        pull_filter=_newer_than_record(target, catalog_dir),
    )
    payload["path"] = str(target)
    return payload
