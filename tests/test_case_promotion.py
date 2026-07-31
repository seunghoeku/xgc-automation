from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
import yaml

from tools import case_promotion as cp
from tools import input_archive as archive


def _write_notebook(path: Path) -> None:
    workbook = openpyxl.Workbook()

    # Modern sheet layout.
    modern = workbook.active
    modern.title = "y2025"
    modern.append(
        ["shot #", "Short description", "Code", "geometry",
         "simulation date", "note", "data"]
    )
    modern.append(
        ["n620fr", "ST40, w/o flow, correct sign", "ES",
         "st40_11013565_flip.node", "2025-10-27", "OK", "kronos"]
    )
    modern.append(
        ["n614fr", "ITER 15MA ES long time simulation", "ES",
         "JINTRAC_15MA_sm5_oct_2025.node", "2025-10-23", "failed", ""]
    )
    modern.append(["empty", "", "", "", "", "", ""])
    modern.append(
        ["n619fr", "ITER 15MA ES original profile", "Es",
         "ITER_15MA_53298.flx.aif", "2025-10-25", "too large flux?", ""]
    )
    # Note whose device (d3d) disagrees with an ASDEX-named archive file.
    modern.append(
        ["n696fr", "d3d PT no field flat", "ES",
         "d193793_torque_pt.node", "2025-06-01", "", ""]
    )
    # EM run from the xgc-eem binary era (no field switch in the input).
    modern.append(
        ["n695pe", "ASDEX EM turbulence", "EM",
         "au37980.node", "2025-05-01", "OK", ""]
    )
    # XGCa (neoclassical) run.
    modern.append(
        ["n694pe", "NT torque neoclassical", "XGCa",
         "d193793_nt.node", "2025-04-01", "Used for Paper", ""]
    )

    # Legacy sheet layout with different headers.
    legacy = workbook.create_sheet("yr2012")
    legacy.append(
        ["Shotname", "Chracteristic", "code version", "Geometry", "Machine",
         "Particle/proc", "#proc", "species", "grid", "general description"]
    )
    legacy.append(
        ["hp98", "re run of pf96", "2012-01-25", "d3d096333", "hopper",
         "40000", "18432", "i", "d3d_16_3mm_whole", "testing of new version"]
    )

    workbook.save(path)


_INPUT_OK = """&sml_param
sml_dt=2.0e-4
sml_mstep=100
sml_outpsi=1.05
sml_restart=.false.
/
&col_param
col_mode=4
/
"""

_INPUT_FAILED = """&sml_param
sml_dt=2.5e-4
sml_mstep=1000
sml_outpsi=1.10
old_removed_parameter=1
/
&col_param
col_mode=0
/
"""


class CasePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)

        self.archive_dir = root / "input_backup"
        self.cache_dir = root / ".cache"
        self.drafts_dir = root / "kb" / "cases" / "drafts"
        self.archive_dir.mkdir()
        self.cache_dir.mkdir()

        (self.cache_dir / "metadata-stellar-test.json").write_text(
            json.dumps(
                {
                    "sml_param": {
                        "parameters": {
                            "sml_dt": {}, "sml_mstep": {},
                            "sml_outpsi": {}, "sml_restart": {},
                        }
                    },
                    "col_param": {"parameters": {"col_mode": {}}},
                }
            ),
            encoding="utf-8",
        )
        (self.archive_dir / "n620fr_ST40_wo_flow").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        (self.archive_dir / "n614fr_ITER_15MA_ES_longtime").write_text(
            _INPUT_FAILED, encoding="utf-8"
        )
        # Archived input with no notebook entry at all.
        (self.archive_dir / "n699pe_mystery").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        # A numbered directory layout with a primary input and variants.
        subdir = self.archive_dir / "n698po_CBC_totalf"
        subdir.mkdir()
        (subdir / "input").write_text(_INPUT_OK, encoding="utf-8")
        (subdir / "input.neo").write_text(_INPUT_OK, encoding="utf-8")
        # A tar variant sitting next to primary files.
        (self.archive_dir / "n697su_CBC_input.tar").write_bytes(
            b"\x00binary tar payload\x00"
        )

        self.notebook = root / "simulation_note_XGC1.xlsx"
        _write_notebook(self.notebook)

        self.patches = [
            patch.object(archive, "ARCHIVE_DIR", self.archive_dir),
            patch.object(archive, "CACHE_DIR", self.cache_dir),
            patch.object(cp, "NOTES_PATH", self.notebook),
            patch.object(cp, "DRAFTS_DIR", self.drafts_dir),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    # -- notebook parsing ---------------------------------------------------

    def test_load_notes_normalizes_modern_and_legacy_sheets(self) -> None:
        notes = cp.load_simulation_notes()
        by_shot = {note.shot: note for note in notes}

        self.assertIn("n620fr", by_shot)
        self.assertEqual(by_shot["n620fr"].number, 620)
        self.assertEqual(by_shot["n620fr"].note, "OK")
        # Legacy header "Chracteristic" maps to description.
        self.assertIn("hp98", by_shot)
        self.assertEqual(by_shot["hp98"].number, 98)
        self.assertEqual(by_shot["hp98"].description, "re run of pf96")
        # Placeholder rows are dropped.
        self.assertNotIn("empty", by_shot)

    def test_notebook_is_not_modified(self) -> None:
        before = self.notebook.read_bytes()
        cp.load_simulation_notes()
        self.assertEqual(self.notebook.read_bytes(), before)

    # -- outcome classification ----------------------------------------------

    def test_classify_outcome_failure_dominates_success(self) -> None:
        result = cp.classify_outcome("OK at first but failed later")
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(result["failure_evidence"])
        self.assertTrue(result["success_evidence"])

    def test_classify_outcome_variants(self) -> None:
        self.assertEqual(cp.classify_outcome("Used for Paper")["outcome"], "success")
        self.assertEqual(cp.classify_outcome("too large flux?")["outcome"], "inconclusive")
        self.assertEqual(cp.classify_outcome("")["outcome"], "unknown")

    def test_negated_success_is_not_success(self) -> None:
        # The n594fr case: "Results are not that good" must not rank as
        # success just because it contains "good".
        result = cp.classify_outcome("Results are not that good")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["success_evidence"], [])
        self.assertTrue(
            any("not that good" in e for e in result["caution_evidence"])
        )
        self.assertEqual(cp.classify_outcome("not good")["outcome"], "inconclusive")
        self.assertEqual(cp.classify_outcome("no good")["outcome"], "inconclusive")
        # Plain positives still work.
        self.assertEqual(cp.classify_outcome("good")["outcome"], "success")
        # Negation plus explicit failure stays failed.
        self.assertEqual(
            cp.classify_outcome("not good, failed at step 10")["outcome"],
            "failed",
        )

    # -- ranking ---------------------------------------------------------------

    def test_ranking_orders_success_before_unknown_before_failed(self) -> None:
        result = cp.rank_promotion_candidates(min_number=600)
        names = [item["archive_name"] for item in result["candidates"]]

        self.assertEqual(
            names,
            ["n620fr_ST40_wo_flow", "n699pe_mystery",
             "n698po_CBC_totalf/input", "n698po_CBC_totalf/input.neo",
             "n697su_CBC_input.tar", "n614fr_ITER_15MA_ES_longtime"],
        )
        failed = result["candidates"][-1]
        self.assertEqual(failed["outcome"], "failed")
        self.assertIn("failed", failed["outcome_evidence"]["failure_evidence"])
        # Notebook rows are carried as evidence.
        self.assertEqual(
            failed["notebook_entries"][0]["description"],
            "ITER 15MA ES long time simulation",
        )

    def test_subdir_input_and_variants_are_classified(self) -> None:
        result = cp.rank_promotion_candidates(min_number=690)
        by_name = {c["archive_name"]: c for c in result["candidates"]}

        # Primary inside a numbered directory: reported as dir/input, no variant.
        self.assertIn("n698po_CBC_totalf/input", by_name)
        self.assertIsNone(by_name["n698po_CBC_totalf/input"]["variant"])
        self.assertEqual(
            by_name["n698po_CBC_totalf/input"]["simulation_number"], 698
        )
        # Its .neo sibling is a variant.
        self.assertEqual(by_name["n698po_CBC_totalf/input.neo"]["variant"], "neo")
        # The tar file is a variant and parses to zero parameters.
        tar = by_name["n697su_CBC_input.tar"]
        self.assertEqual(tar["variant"], "tar")
        # Variants rank below primaries within the same outcome group.
        names = [c["archive_name"] for c in result["candidates"]]
        self.assertLess(
            names.index("n698po_CBC_totalf/input"),
            names.index("n698po_CBC_totalf/input.neo"),
        )

    def test_name_note_mismatch_detected(self) -> None:
        (self.archive_dir / "n696fr_ASDEX_I_mode_xgca").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        result = cp.rank_promotion_candidates(min_number=696, max_files=10)
        record = next(
            c for c in result["candidates"]
            if c["archive_name"] == "n696fr_ASDEX_I_mode_xgca"
        )
        mismatch = record["name_note_mismatch"]
        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["filename_device"], "asdex")
        self.assertEqual(mismatch["notebook_device"], "d3d")
        # And the matching case: ST40 file vs ST40 note -> no mismatch.
        ok = next(
            c for c in cp.rank_promotion_candidates(min_number=620)["candidates"]
            if c["archive_name"] == "n620fr_ST40_wo_flow"
        )
        self.assertIsNone(ok["name_note_mismatch"])

    # -- binary era (xgc-eem / xgc-es / xgca) ----------------------------------

    def test_classify_code_column(self) -> None:
        self.assertEqual(
            cp.classify_code_column("EM"),
            {"flavor": "xgc1", "field_model": "em",
             "binary_era": "xgc-eem", "code_text": "EM"},
        )
        self.assertEqual(cp.classify_code_column("ES/adiabatic")["binary_era"], "xgc-es")
        self.assertEqual(cp.classify_code_column("EM - df")["field_model"], "em")
        self.assertEqual(cp.classify_code_column("XGCa")["binary_era"], "xgca")
        self.assertEqual(cp.classify_code_column("xgca")["flavor"], "xgca")
        self.assertEqual(cp.classify_code_column("")["flavor"], "unknown")
        # ES/ EM tokens inside other words must not match.
        self.assertEqual(cp.classify_code_column("core_dev")["field_model"], "unknown")

    def test_em_era_input_without_switch_warns_on_promotion(self) -> None:
        (self.archive_dir / "n695pe_ASDEX_EM_turb").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        plan = cp.plan_case_promotion("n695pe_ASDEX_EM_turb")
        field = plan["draft"]["physics_inferred"]["field_model"]

        self.assertEqual(field["value"], "electromagnetic")
        self.assertIn("xgc-eem", field["source"])
        self.assertTrue(
            any("sml_electromagnetic=.true." in w for w in plan["warnings"])
        )

    def test_em_input_with_explicit_switch_does_not_warn(self) -> None:
        (self.archive_dir / "n695pe_ASDEX_EM_explicit").write_text(
            _INPUT_OK.replace(
                "sml_restart=.false.",
                "sml_restart=.false.\nsml_electromagnetic=.true.",
            ),
            encoding="utf-8",
        )
        plan = cp.plan_case_promotion("n695pe_ASDEX_EM_explicit")
        field = plan["draft"]["physics_inferred"]["field_model"]

        self.assertEqual(field["value"], "electromagnetic")
        self.assertIn("input parameter", field["source"])
        self.assertFalse(
            any("silently run the wrong physics" in w for w in plan["warnings"])
        )

    def test_xgca_run_is_flagged(self) -> None:
        (self.archive_dir / "n694pe_NT_torque_xgca").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        plan = cp.plan_case_promotion("n694pe_NT_torque_xgca")

        self.assertEqual(
            plan["draft"]["physics_inferred"]["flavor"]["value"], "xgca"
        )
        self.assertTrue(any("xgca" in w for w in plan["warnings"]))

    def test_ranking_exposes_binary_era(self) -> None:
        (self.archive_dir / "n695pe_ASDEX_EM_turb").write_text(
            _INPUT_OK, encoding="utf-8"
        )
        result = cp.rank_promotion_candidates(min_number=695, max_files=5)
        record = next(
            c for c in result["candidates"]
            if c["archive_name"] == "n695pe_ASDEX_EM_turb"
        )
        self.assertEqual(record["binary_era"]["binary_era"], "xgc-eem")

    # -- planning ---------------------------------------------------------------

    def test_plan_infers_physics_with_sources(self) -> None:
        plan = cp.plan_case_promotion("n620fr_ST40_wo_flow")
        physics = plan["draft"]["physics_inferred"]

        self.assertEqual(physics["field_model"]["value"], "electrostatic")
        self.assertIn("Code column", physics["field_model"]["source"])
        self.assertEqual(physics["domain"]["value"], "edge-including")
        self.assertEqual(physics["collisions"]["value"], "col_mode=4")
        self.assertEqual(
            physics["marker_model"]["value"],
            "TotalF (XGC default; nothing declared)",
        )
        self.assertEqual(plan["outcome"], "success")
        self.assertEqual(
            plan["draft"]["description"], "ST40, w/o flow, correct sign"
        )

    def test_plan_warns_on_unknown_and_schema_gaps(self) -> None:
        plan = cp.plan_case_promotion("n699pe_mystery")
        self.assertEqual(plan["outcome"], "unknown")
        self.assertTrue(any("No notebook entry" in w for w in plan["warnings"]))

        plan_failed = cp.plan_case_promotion("n614fr_ITER_15MA_ES_longtime")
        self.assertTrue(
            any("old_removed_parameter" in w for w in plan_failed["warnings"])
        )

    def test_plan_unknown_archive_raises(self) -> None:
        with self.assertRaises(cp.CasePromotionError):
            cp.plan_case_promotion("n000xx_does_not_exist")

    # -- writing drafts --------------------------------------------------------

    def test_write_requires_confirmation(self) -> None:
        result = cp.write_draft_case("n620fr_ST40_wo_flow")
        self.assertFalse(result["written"])
        self.assertFalse(self.drafts_dir.exists())

    def test_write_refuses_failed_run_without_allow_failed(self) -> None:
        with self.assertRaises(cp.CasePromotionError):
            cp.write_draft_case(
                "n614fr_ITER_15MA_ES_longtime", confirm=True
            )
        result = cp.write_draft_case(
            "n614fr_ITER_15MA_ES_longtime", confirm=True, allow_failed=True
        )
        self.assertTrue(result["written"])

    def test_write_creates_draft_and_refuses_overwrite(self) -> None:
        result = cp.write_draft_case("n620fr_ST40_wo_flow", confirm=True)
        self.assertTrue(result["written"])

        draft = yaml.safe_load(Path(result["draft_path"]).read_text())
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["promotion"]["simulation_number"], 620)
        self.assertEqual(draft["promotion"]["outcome"]["outcome"], "success")
        self.assertIn("remaining_steps", draft)

        with self.assertRaises(cp.CasePromotionError):
            cp.write_draft_case("n620fr_ST40_wo_flow", confirm=True)

    def test_drafts_are_invisible_to_list_cases(self) -> None:
        from tools import input_builder

        cp.write_draft_case("n620fr_ST40_wo_flow", confirm=True)
        cases_dir = self.drafts_dir.parent
        with patch.object(input_builder, "CASES_DIR", cases_dir):
            names = [case["name"] for case in input_builder.list_cases()]
        self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main()
