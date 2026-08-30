from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import case_promotion as cp
from tools import legacy_import
from tools import simulation_catalog as sc


def note(shot: str, **kwargs: str) -> cp.SimulationNote:
    defaults = {
        "sheet": "y2024",
        "description": "",
        "code": "",
        "geometry": "",
        "note": "",
        "date": "",
        "data": "",
    }
    defaults.update(kwargs)
    return cp.SimulationNote(
        shot=shot,
        number=cp._shot_number(shot),
        **defaults,  # type: ignore[arg-type]
    )


class BuildRecordTests(unittest.TestCase):
    def test_modern_shot_infers_machine_from_the_code_suffix(self) -> None:
        record = legacy_import.build_record(
            note("n634pe", description="TCV NT deltaf", date="2026-01-23 00:00:00")
        )
        assert record is not None
        self.assertEqual(record["record_id"], "n634pe")
        self.assertEqual(record["machines"], ["perlmutter"])
        self.assertEqual(record["number"], 634)
        self.assertEqual(record["origin"], "notebook")
        self.assertEqual(record["started_on"], "2026-01-23")

    def test_legacy_prefix_shot_keeps_its_own_key(self) -> None:
        record = legacy_import.build_record(note("ti204", sheet="y2014"))
        assert record is not None
        self.assertEqual(record["record_id"], "ti204")
        self.assertIsNone(record["simulation_id"])
        self.assertEqual(record["machines"], ["titan"])
        self.assertEqual(record["machine_basis"], "legacy shot name prefix")
        self.assertEqual(record["started_on"], "2014-01-01")

    def test_machine_inference_states_its_basis(self) -> None:
        modern = legacy_import.build_record(note("n634pe"))
        assert modern is not None
        self.assertEqual(modern["machine_basis"], "shot name machine code")

    def test_abbreviated_shot_recovers_the_machine_from_the_description(self) -> None:
        # The notebook sometimes writes "n555" in the shot column but the full
        # "n555pe_..." in the description.
        record = legacy_import.build_record(
            note("n555", description="n555pe_ASDEX_I_mode_turb_ES")
        )
        assert record is not None
        self.assertEqual(record["machines"], ["perlmutter"])
        self.assertEqual(record["machine_basis"], "full shot name in the description")

    def test_a_different_number_in_the_description_is_not_borrowed(self) -> None:
        record = legacy_import.build_record(
            note("n555", description="restart of n432po with more particles")
        )
        assert record is not None
        self.assertEqual(record["machines"], [])

    def test_archive_detection_tolerates_the_notebook_wording(self) -> None:
        for value in ("hpss", "HPSS nersc", "on tape"):
            with self.subTest(value=value):
                record = legacy_import.build_record(note("n700pe", data=value))
                assert record is not None
                self.assertEqual(record["archive"]["system"], "hpss")
                self.assertEqual(record["archive"]["notebook_value"], value)

    def test_unarchived_run_claims_no_archive_system(self) -> None:
        record = legacy_import.build_record(note("n700pe", data="on scratch"))
        assert record is not None
        self.assertIsNone(record["archive"]["system"])

    def test_ambiguous_prefix_claims_no_machine(self) -> None:
        # "pf" is not a machine code anyone can vouch for; leaving it blank is
        # better than asserting a machine the run may never have touched.
        record = legacy_import.build_record(note("pf42", sheet="yr2010"))
        assert record is not None
        self.assertEqual(record["machines"], [])
        self.assertIsNone(record["machine_basis"])

    def test_notebook_failure_wording_becomes_a_failed_status(self) -> None:
        record = legacy_import.build_record(
            note("n600pe", note="failed due to numerical instability")
        )
        assert record is not None
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["legacy"]["outcome"], "failed")
        self.assertTrue(record["legacy"]["outcome_evidence"]["failure"])

    def test_hpss_in_the_data_column_marks_the_record_archived(self) -> None:
        record = legacy_import.build_record(note("n635pe", data="hpss"))
        assert record is not None
        self.assertEqual(record["status"], "archived")
        self.assertEqual(record["archive"]["system"], "hpss")
        # The notebook says it went to tape, not where; do not invent a path.
        self.assertIsNone(record["archive"]["path"])

    def test_researcher_wording_is_preserved_verbatim(self) -> None:
        record = legacy_import.build_record(
            note(
                "n634pe",
                description="n634pe_TCV_NT_deltaf",
                note="TCV benchmark. preliminary. lower growth rate",
                geometry="tcv68954.node",
                code="ES/adiabatic",
            )
        )
        assert record is not None
        legacy = record["legacy"]
        self.assertEqual(legacy["note"], "TCV benchmark. preliminary. lower growth rate")
        self.assertEqual(legacy["geometry"], "tcv68954.node")
        self.assertEqual(legacy["source"], "Other_data/simulation_note_XGC1.xlsx")
        # The human note field stays empty: the researcher never wrote one here.
        self.assertEqual(record["note"], "")

    def test_annotation_rows_are_not_simulations(self) -> None:
        self.assertIsNone(
            legacy_import.build_record(
                note("Numbering scheme has changed. n + [NUMBER] + [MACHINE PREFIX]")
            )
        )

    def test_decorated_shot_cell_keeps_the_leading_identifier(self) -> None:
        record = legacy_import.build_record(note("n585fr (dup number)"))
        assert record is not None
        self.assertEqual(record["record_id"], "n585fr")
        self.assertEqual(record["legacy"]["shot"], "n585fr (dup number)")


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.notes = [
            note("n634pe", description="TCV NT deltaf", data="hpss"),
            note("ti204", description="old d3d run", sheet="y2014"),
            note("empty"),
        ]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_plan_writes_nothing(self) -> None:
        plan = legacy_import.plan_import(notes=self.notes, catalog_dir=self.dir)
        self.assertFalse(plan["writes"])
        self.assertEqual(plan["counts"]["create"], 2)
        self.assertEqual(plan["counts"]["skip"], 1)
        self.assertEqual(list(self.dir.glob("*.yaml")), [])

    def test_import_without_confirm_writes_nothing(self) -> None:
        result = legacy_import.import_notebook(notes=self.notes, catalog_dir=self.dir)
        self.assertFalse(result["imported"])
        self.assertEqual(list(self.dir.glob("*.yaml")), [])

    def test_confirmed_import_writes_records(self) -> None:
        result = legacy_import.import_notebook(
            notes=self.notes, catalog_dir=self.dir, confirm=True
        )
        self.assertTrue(result["imported"])
        self.assertEqual(sorted(result["written"]), ["n634pe", "ti204"])
        self.assertTrue((self.dir / "n634pe.yaml").is_file())

    def test_import_is_idempotent(self) -> None:
        legacy_import.import_notebook(notes=self.notes, catalog_dir=self.dir, confirm=True)
        again = legacy_import.import_notebook(
            notes=self.notes, catalog_dir=self.dir, confirm=True
        )
        self.assertEqual(again["written"], [])
        self.assertEqual(again["counts"]["skip"], 3)

    def test_overwrite_refreshes_previously_imported_records(self) -> None:
        legacy_import.import_notebook(notes=self.notes, catalog_dir=self.dir, confirm=True)
        refreshed = legacy_import.import_notebook(
            notes=self.notes, catalog_dir=self.dir, confirm=True, overwrite=True
        )
        self.assertEqual(sorted(refreshed["written"]), ["n634pe", "ti204"])

    def test_notebook_never_overwrites_an_automation_record(self) -> None:
        sc.create_record(
            "n634pe", catalog_dir=self.dir, machines=["perlmutter"], short_name="real run"
        )
        result = legacy_import.import_notebook(
            notes=self.notes, catalog_dir=self.dir, confirm=True, overwrite=True
        )
        self.assertEqual(result["written"], ["ti204"])
        reasons = [entry["reason"] for entry in result["skip"]]
        self.assertTrue(any("automation-owned" in reason for reason in reasons))
        self.assertEqual(
            sc.read_record("n634pe", catalog_dir=self.dir)["short_name"], "real_run"
        )

    def test_duplicate_shot_within_one_import_is_reported(self) -> None:
        notes = [note("n634pe"), note("n634pe", description="second row")]
        plan = legacy_import.plan_import(notes=notes, catalog_dir=self.dir)
        self.assertEqual(plan["counts"]["create"], 1)
        self.assertEqual(plan["counts"]["skip"], 1)
        self.assertIn("duplicate", plan["skip"][0]["reason"])

    def test_legacy_numbers_that_collide_stay_separate_records(self) -> None:
        # ti204 and su204 share a number but are different simulations; the
        # catalog is keyed by the shot string so both survive.
        notes = [note("ti204"), note("su204")]
        result = legacy_import.import_notebook(
            notes=notes, catalog_dir=self.dir, confirm=True
        )
        self.assertEqual(sorted(result["written"]), ["su204", "ti204"])


class RealNotebookImportTests(unittest.TestCase):
    """Exercise the import against the researcher's actual notebook if present."""

    def setUp(self) -> None:
        if not cp.NOTES_PATH.is_file():
            self.skipTest("simulation notebook is personal data and not present")
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_import_covers_most_of_the_notebook(self) -> None:
        plan = legacy_import.plan_import(catalog_dir=self.dir)
        self.assertGreater(plan["notes_read"], 500)
        # A handful of rows are annotations rather than shots; the rest must
        # convert, otherwise the historical corpus is not searchable.
        self.assertGreater(plan["counts"]["create"], plan["notes_read"] * 0.95)

    def test_imported_records_are_searchable(self) -> None:
        legacy_import.import_notebook(catalog_dir=self.dir, confirm=True)
        matches = sc.search("tcv", catalog_dir=self.dir)
        self.assertTrue(matches)
        self.assertTrue(all(row["origin"] == "notebook" for row in matches))


if __name__ == "__main__":
    unittest.main()
