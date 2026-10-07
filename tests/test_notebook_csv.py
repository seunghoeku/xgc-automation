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


class StaleCsvTests(unittest.TestCase):
    """A CSV older than the record must not overwrite a newer catalog note."""

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
        notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _edit_csv_note(self, text: str) -> None:
        with self.path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        column = rows[0].index("note")
        rows[1][column] = text
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)

    def _set_mtimes(self, *, csv_time: float, record_time: float) -> None:
        import os

        os.utime(self.path, (csv_time, csv_time))
        record = sc.record_path("n651po", catalog_dir=self.catalog_dir)
        os.utime(record, (record_time, record_time))

    def test_csv_newer_than_record_pulls_the_note(self) -> None:
        self._edit_csv_note("typed in the spreadsheet")
        self._set_mtimes(csv_time=2_000_000_000, record_time=1_000_000_000)
        payload = notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)
        self.assertEqual(payload["pulled_notes"], ["n651po"])
        self.assertEqual(payload["stale_notes"], [])
        record = sc.read_record("n651po", catalog_dir=self.catalog_dir)
        self.assertEqual(record["note"], "typed in the spreadsheet")

    def test_csv_older_than_record_is_stale_and_overwritten(self) -> None:
        self._edit_csv_note("left over from before git pull")
        sc.set_note("n651po", "written with xgc-sim note", catalog_dir=self.catalog_dir)
        self._set_mtimes(csv_time=1_000_000_000, record_time=2_000_000_000)
        payload = notebook_csv.sync(path=self.path, catalog_dir=self.catalog_dir, confirm=True)
        self.assertEqual(payload["pulled_notes"], [])
        self.assertEqual([item["record_id"] for item in payload["stale_notes"]], ["n651po"])
        record = sc.read_record("n651po", catalog_dir=self.catalog_dir)
        self.assertEqual(record["note"], "written with xgc-sim note")
        with self.path.open(newline="", encoding="utf-8") as handle:
            rows = {row["record_id"]: row for row in csv.DictReader(handle)}
        self.assertEqual(rows["n651po"]["note"], "written with xgc-sim note")
