from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from tools import sheet_sync
from tools import simulation_catalog as sc


class FakeWorksheet:
    """An in-memory stand-in for the gspread worksheet API.

    Sheet sync must be testable without a Google credential or a network, since
    the whole point of the design is that the sheet is an optional replica.

    The signatures mirror gspread 6 exactly, including the values-before-range
    order of ``update``, which is reversed from gspread 5. Calling it
    positionally against the wrong version would write to the wrong cells
    without erroring, so both parameters are keyword-only here.
    """

    def __init__(self, values: list[list[str]] | None = None) -> None:
        self.values = values or []
        self.resized: tuple[int, int] | None = None
        self.updates: list[tuple[str, list[list[Any]]]] = []

    def get_all_values(self) -> list[list[str]]:
        return [list(row) for row in self.values]

    def update(self, *, values: list[list[Any]], range_name: str) -> None:
        self.updates.append((range_name, values))
        self.values = [[str(cell) for cell in row] for row in values]

    def resize(self, *, rows: int, cols: int) -> None:
        self.resized = (rows, cols)


def sheet_from(rows: list[dict[str, str]]) -> FakeWorksheet:
    header = list(sheet_sync.COLUMNS)
    values = [header]
    for row in rows:
        values.append([row.get(column, "") for column in header])
    return FakeWorksheet(values)


class ResolveSettingsTests(unittest.TestCase):
    def test_environment_overrides_and_reports_missing_pieces(self) -> None:
        settings = sheet_sync.resolve_settings(
            credentials="/nope/key.json", spreadsheet_id="sheet-1"
        )
        self.assertEqual(settings["spreadsheet_id"], "sheet-1")
        self.assertEqual(settings["worksheet"], sheet_sync.DEFAULT_WORKSHEET)
        self.assertTrue(settings["configured"])
        self.assertFalse(settings["credentials_exist"])

    def test_missing_configuration_is_named_not_guessed(self) -> None:
        settings = sheet_sync.resolve_settings(credentials="/nope/key.json")
        # Without an id there is nothing to sync to; say so rather than
        # inventing a spreadsheet.
        self.assertIn("spreadsheet_id", settings["missing"])
        self.assertFalse(settings["configured"])

    def test_open_worksheet_refuses_when_unconfigured(self) -> None:
        with self.assertRaises(sheet_sync.SheetSyncError):
            sheet_sync.open_worksheet(credentials=None, spreadsheet_id=None)

    def test_cli_check_reports_configuration_without_connecting(self) -> None:
        # `xgc-sim sync-sheet --check` must say the mirror is unconfigured
        # rather than pretending it is healthy, and must not exit 0 if it is.
        from tools import simcat_cli

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = simcat_cli.main(["--json", "sync-sheet", "--check"])
        payload = json.loads(buffer.getvalue())

        self.assertEqual(code, 0 if payload["configured"] else 1)
        self.assertIn("worksheet", payload)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        sc.create_record(
            "n645pe",
            catalog_dir=self.dir,
            machines=["perlmutter"],
            short_name="ASDEX I-mode",
            status="completed",
            case="asdex-imode",
        )
        sc.create_record(
            "n646po",
            catalog_dir=self.dir,
            machines=["polaris"],
            short_name="ST40 heat load",
            status="running",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plan_against_an_empty_sheet_pushes_everything(self) -> None:
        plan = sheet_sync.plan_sync(worksheet=FakeWorksheet(), catalog_dir=self.dir)
        self.assertEqual(plan["counts"]["push_new"], 2)
        self.assertEqual(plan["counts"]["note_pulls"], 0)
        self.assertFalse(plan["writes"])

    def test_sync_without_confirm_writes_nothing(self) -> None:
        worksheet = FakeWorksheet()
        result = sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir)
        self.assertFalse(result["synced"])
        self.assertEqual(worksheet.updates, [])

    def test_confirmed_sync_pushes_a_header_and_one_row_per_simulation(self) -> None:
        worksheet = FakeWorksheet()
        result = sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        self.assertTrue(result["synced"])
        self.assertEqual(result["pushed_rows"], 2)
        self.assertEqual(worksheet.values[0], list(sheet_sync.COLUMNS))
        self.assertEqual(len(worksheet.values), 3)
        self.assertEqual(worksheet.resized, (3, len(sheet_sync.COLUMNS)))
        self.assertEqual(worksheet.updates[0][0], "A1")

    def test_worksheet_protocol_matches_the_installed_gspread(self) -> None:
        # gspread 6 reversed update()'s parameter order; a positional call
        # against the wrong version writes to the wrong cells silently.
        try:
            from gspread.worksheet import Worksheet as RealWorksheet
        except ImportError:
            self.skipTest("gspread is an optional dependency and is not installed")
        import inspect

        parameters = list(inspect.signature(RealWorksheet.update).parameters)
        self.assertIn("values", parameters)
        self.assertIn("range_name", parameters)
        self.assertLess(parameters.index("values"), parameters.index("range_name"))

    def test_note_edited_in_the_sheet_is_pulled_into_the_catalog(self) -> None:
        worksheet = sheet_from(
            [{"record_id": "n645pe", "note": "matches the GENE benchmark"}]
        )
        result = sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        self.assertEqual(result["pulled_notes"], ["n645pe"])
        self.assertEqual(
            sc.read_record("n645pe", catalog_dir=self.dir)["note"],
            "matches the GENE benchmark",
        )

    def test_pulled_note_survives_the_push_that_follows(self) -> None:
        worksheet = sheet_from([{"record_id": "n645pe", "note": "human wrote this"}])
        sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        note_index = list(sheet_sync.COLUMNS).index("note")
        pushed = {row[0]: row[note_index] for row in worksheet.values[1:]}
        self.assertEqual(pushed["n645pe"], "human wrote this")

    def test_machine_fields_edited_in_the_sheet_are_overwritten(self) -> None:
        # The sheet is a replica: a human retyping the status must not become
        # the truth, because nothing verified it.
        worksheet = sheet_from([{"record_id": "n646po", "status": "completed"}])
        sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        status_index = list(sheet_sync.COLUMNS).index("status")
        pushed = {row[0]: row[status_index] for row in worksheet.values[1:]}
        self.assertEqual(pushed["n646po"], "running")
        self.assertEqual(sc.read_record("n646po", catalog_dir=self.dir)["status"], "running")

    def test_no_pull_ignores_sheet_notes(self) -> None:
        worksheet = sheet_from([{"record_id": "n645pe", "note": "typed in browser"}])
        result = sheet_sync.sync(
            worksheet=worksheet, catalog_dir=self.dir, pull_notes=False, confirm=True
        )
        self.assertEqual(result["pulled_notes"], [])
        self.assertEqual(sc.read_record("n645pe", catalog_dir=self.dir)["note"], "")

    def test_rows_only_in_the_sheet_are_reported_not_silently_dropped(self) -> None:
        worksheet = sheet_from([{"record_id": "n999xx", "note": "who is this"}])
        plan = sheet_sync.plan_sync(worksheet=worksheet, catalog_dir=self.dir)
        self.assertEqual(plan["sheet_only"], ["n999xx"])

    def test_reordered_sheet_columns_still_join_correctly(self) -> None:
        header = ["note", "status", "record_id"]
        worksheet = FakeWorksheet([header, ["from the sheet", "wrong", "n645pe"]])
        result = sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        self.assertEqual(result["pulled_notes"], ["n645pe"])

    def test_sheet_without_the_key_column_is_rejected(self) -> None:
        worksheet = FakeWorksheet([["number", "note"], ["645", "x"]])
        with self.assertRaises(sheet_sync.SheetSyncError):
            sheet_sync.plan_sync(worksheet=worksheet, catalog_dir=self.dir)

    def test_shrinking_catalog_resizes_so_stale_rows_cannot_linger(self) -> None:
        worksheet = sheet_from(
            [{"record_id": f"n{600 + i}pe"} for i in range(10)]
        )
        sheet_sync.sync(worksheet=worksheet, catalog_dir=self.dir, confirm=True)
        self.assertEqual(worksheet.resized, (3, len(sheet_sync.COLUMNS)))
        self.assertEqual(len(worksheet.values), 3)


if __name__ == "__main__":
    unittest.main()
