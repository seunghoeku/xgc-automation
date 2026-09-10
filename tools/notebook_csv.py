"""Mirror the simulation catalog to a CSV notebook in the repository.

``Other_data/simulation_note_XGC1.xlsx`` was the researcher's hand-kept record
of every numbered run. It stays as the historical notebook (rows through
n656po), but it is a poor target for automation: openpyxl rewrites drop the
cached values of old formulas, and a binary workbook shows no diff in git.

``kb/simulation_notebook.csv`` replaces it going forward. It is the same view
-- one row per simulation, the human ``note`` column last -- but plain text,
diffable, and regenerated from the catalog after every submission. It reuses
``tools.sheet_sync`` unchanged: a CSV file is just another worksheet.

    catalog (kb/simulations/*.yaml)  --push-->  kb/simulation_notebook.csv
    catalog                          <--pull--  note column only

Edit the ``note`` cell of a row in any spreadsheet program, save as CSV, and
the next sync (or the next submission) pulls it into the catalog record. Every
other column is overwritten from the catalog; a hand-typed value there cannot
be verified and does not survive.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from tools import sheet_sync

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK_PATH = PROJECT_ROOT / "kb" / "simulation_notebook.csv"


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


def plan(*, path: Path | None = None, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Diff the catalog against the CSV. Writes nothing."""
    worksheet = CsvWorksheet(path or NOTEBOOK_PATH)
    return sheet_sync.plan_sync(worksheet=worksheet, catalog_dir=catalog_dir)


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
    )
    payload["path"] = str(target)
    return payload
