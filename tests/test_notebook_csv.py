from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from tools import notebook_csv
from tools import sheet_sync
from tools import simulation_catalog as sc


class CsvNotebookTests(unittest.TestCase):
    """The CSV notebook is a replica of the catalog with one human column."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog_dir = self.root / "simulations"
        self.path = self.root / "simulation_notebook.csv"
        sc.create_record(
            "n651po",
            catalog_dir=self.catalog_dir,
            machines=["polaris"],
            short_name="dadt_ecbc_df",
            directory_name="n651po_dadt_ecbc_df",
            tags=["dadt"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rows(self) -> dict[str, dict[str, str]]:
        with self.path.open(newline="", encoding="utf-8") as handle:
            return {row["record_id"]: row for row in csv.DictReader(handle)}

    def test_plan_without_confirm_writes_nothing(self) -> None:
        payload = notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir)
        self.assertFalse(payload["synced"])
        self.assertFalse(self.path.exists())
        self.assertEqual(payload["push_new"], ["n651po"])

    def test_sync_creates_the_csv_with_the_sheet_columns(self) -> None:
        payload = notebook_csv.sync(
            path=self.path, catalog_dir=self.catalog_dir, confirm=True
        )
        self.assertTrue(payload["synced"])
        self.assertEqual(payload["pushed_rows"], 1)
        with self.path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        self.assertEqual(header, list(sheet_sync.COLUMNS))
        row = self._rows()["n651po"]
        self.assertEqual(row["machine"], "polaris")
        self.assertEqual(row["status"], "reserved")
        self.assertEqual(row["short_name"], "dadt_ecbc_df")

    def test_note_edited_in_the_csv_is_pulled_into_the_catalog(self) -> None:
        notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)
        rows = self._rows()
        rows["n651po"]["note"] = "n=0 potential runs away"
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sheet_sync.COLUMNS))
            writer.writeheader()
            writer.writerows(rows.values())

        payload = notebook_csv.sync(
            path=self.path, catalog_dir=self.catalog_dir, confirm=True
        )
        self.assertEqual(payload["pulled_notes"], ["n651po"])
        record = sc.read_record("n651po", catalog_dir=self.catalog_dir)
        self.assertEqual(record["note"], "n=0 potential runs away")
        self.assertEqual(self._rows()["n651po"]["note"], "n=0 potential runs away")

    def test_machine_columns_edited_by_hand_are_overwritten(self) -> None:
        notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)
        rows = self._rows()
        rows["n651po"]["status"] = "completed"
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sheet_sync.COLUMNS))
            writer.writeheader()
            writer.writerows(rows.values())
        notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)
        self.assertEqual(self._rows()["n651po"]["status"], "reserved")

    def test_a_row_the_catalog_no_longer_has_is_dropped(self) -> None:
        worksheet = notebook_csv.CsvWorksheet(self.path)
        worksheet.update(
            values=[list(sheet_sync.COLUMNS), ["ghost"] + [""] * (len(sheet_sync.COLUMNS) - 1)],
            range_name="A1",
        )
        payload = notebook_csv.sync(
            path=self.path, catalog_dir=self.catalog_dir, confirm=True
        )
        self.assertEqual(payload["sheet_only"], ["ghost"])
        self.assertNotIn("ghost", self._rows())


if __name__ == "__main__":
    unittest.main()
