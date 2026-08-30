from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import run_adoption
from tools import simulation_catalog as sc


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.catalog_dir = self.root / "simulations"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_run(
        self,
        run_id: str,
        *,
        provenance_class: str = "master",
        **fields: Any,
    ) -> Path:
        record = {
            "run_id": run_id,
            "machine": "perlmutter",
            "job_id": "1",
            "submitted_at": "2026-08-01T00:00:00+00:00",
        }
        record.update(fields)
        path = self.runs / provenance_class / run_id / "provenance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        return run_adoption.plan_adoption(
            runs_dir=self.runs, catalog_dir=self.catalog_dir, **kwargs
        )

    def adopt(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("confirm", True)
        return run_adoption.adopt_runs(
            runs_dir=self.runs, catalog_dir=self.catalog_dir, **kwargs
        )

    def test_restarts_of_one_simulation_become_one_record(self) -> None:
        # The same directory resubmitted twice is one simulation, two runs.
        self.write_run("run-a", case_dir="/pscratch/work/tcv_reduce_adia_k1")
        self.write_run("run-b", case_dir="/pscratch/work/tcv_reduce_adia_k1")

        plan = self.plan()

        self.assertEqual(plan["counts"]["create"], 1)
        self.assertEqual(plan["create"][0]["record_id"], "tcv_reduce_adia_k1")
        self.assertEqual(plan["create"][0]["runs"], 2)

    def test_different_directories_stay_separate_simulations(self) -> None:
        self.write_run("run-a", case_dir="/pscratch/work/tcv_k1")
        self.write_run("run-b", case_dir="/pscratch/work/tcv_off")
        self.assertEqual(self.plan()["counts"]["create"], 2)

    def test_working_subdirectory_does_not_split_a_simulation(self) -> None:
        # ".../k1" and ".../k1/run" are the same work.
        self.write_run("run-a", case_dir="/grand/proj/dev_reduce_adia/k1")
        self.write_run("run-b", case_dir="/grand/proj/dev_reduce_adia/k1/run")

        plan = self.plan()

        self.assertEqual(plan["counts"]["create"], 1)
        self.assertEqual(plan["create"][0]["runs"], 2)

    def test_ambiguous_leaf_name_is_qualified_by_its_campaign(self) -> None:
        self.write_run("run-a", case_dir="/grand/proj/dev_reduce_adia/k1")
        self.assertEqual(self.plan()["create"][0]["record_id"], "dev_reduce_adia_k1")

    def test_descriptive_leaf_name_is_left_alone(self) -> None:
        self.write_run("run-a", case_dir="/pscratch/codex_work/tcv_xgca_reduce_adia_k1")
        self.assertEqual(
            self.plan()["create"][0]["record_id"], "tcv_xgca_reduce_adia_k1"
        )

    def test_same_directory_name_on_two_machines_is_disambiguated(self) -> None:
        self.write_run("run-a", machine="perlmutter", case_dir="/a/tcv_scan_alpha")
        self.write_run("run-b", machine="polaris", case_dir="/b/tcv_scan_alpha")

        ids = sorted(entry["record_id"] for entry in self.plan()["create"])

        self.assertEqual(ids, ["tcv_scan_alpha_perlmutter", "tcv_scan_alpha_polaris"])

    def test_remote_run_dir_is_used_when_there_is_no_case_dir(self) -> None:
        self.write_run("run-a", remote_run_dir="/pscratch/xgc-runs/ecbc_short_64")
        self.assertEqual(self.plan()["create"][0]["record_id"], "ecbc_short_64")

    def test_run_already_linked_to_a_simulation_is_left_alone(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_k1", simulation_id="n645pe")

        plan = self.plan()

        self.assertEqual(plan["counts"]["create"], 0)
        self.assertIn("already linked", plan["skip"][0]["reason"])

    def test_smoke_tests_are_excluded_by_default(self) -> None:
        self.write_run("pbs-smoke-20260730", case_dir="/a/xgc-runs/pbs-smoke-20260730")

        plan = self.plan()

        self.assertEqual(plan["counts"]["create"], 0)
        self.assertIn("smoke test", plan["skip"][0]["reason"])

    def test_smoke_tests_can_be_adopted_on_request(self) -> None:
        self.write_run("pbs-smoke-20260730", case_dir="/a/xgc-runs/pbs-smoke-20260730")
        self.assertEqual(
            self.plan(include_smoke_tests=True)["counts"]["create"], 1
        )

    def test_run_without_any_directory_cannot_be_grouped(self) -> None:
        self.write_run("run-a")
        plan = self.plan()
        self.assertEqual(plan["counts"]["create"], 0)
        self.assertIn("no case_dir", plan["skip"][0]["reason"])

    def test_corrupt_provenance_is_reported_not_fatal(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_good_case")
        bad = self.runs / "master" / "run-bad" / "provenance.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not json", encoding="utf-8")

        plan = self.plan()

        self.assertEqual(plan["counts"]["create"], 1)
        self.assertTrue(any("unreadable" in entry["reason"] for entry in plan["skip"]))

    def test_plan_writes_nothing(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_k1_case")
        plan = self.plan()
        self.assertFalse(plan["writes"])
        self.assertFalse(self.catalog_dir.exists())

    def test_adoption_without_confirm_writes_nothing(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_k1_case")
        result = self.adopt(confirm=False)
        self.assertFalse(result["adopted"])
        self.assertFalse(self.catalog_dir.exists())

    def test_confirmed_adoption_writes_a_searchable_record(self) -> None:
        self.write_run(
            "run-a",
            case_dir="/pscratch/work/tcv_reduce_adia_k1",
            remote_run_dir="/pscratch/work/tcv_reduce_adia_k1",
            job_id="56418578",
            input_sha256="abc123",
            xgc={"commit": "138c6e1f", "exe": "/bin/xgca"},
        )

        self.adopt()

        record = sc.read_record("tcv_reduce_adia_k1", catalog_dir=self.catalog_dir)
        self.assertEqual(record["origin"], "adopted")
        self.assertEqual(record["machines"], ["perlmutter"])
        self.assertEqual(record["runs"][0]["job_id"], "56418578")
        self.assertEqual(record["input_sha256"], "abc123")
        self.assertEqual(record["xgc_commit"], "138c6e1f")
        self.assertEqual(
            record["remote_dirs"]["perlmutter"], "/pscratch/work/tcv_reduce_adia_k1"
        )
        matches = sc.search("reduce_adia", catalog_dir=self.catalog_dir)
        self.assertEqual([row["record_id"] for row in matches], ["tcv_reduce_adia_k1"])

    def test_adopted_record_claims_no_sequence_number(self) -> None:
        # Numbers come from the registry by human decision; adoption must not
        # mint them retroactively.
        self.write_run("run-a", case_dir="/pscratch/work/tcv_reduce_adia_k1")
        self.adopt()

        record = sc.read_record("tcv_reduce_adia_k1", catalog_dir=self.catalog_dir)

        self.assertIsNone(record["number"])
        self.assertIsNone(record["simulation_id"])
        self.assertFalse(record["adoption"]["numbered"])

    def test_status_does_not_invent_an_outcome(self) -> None:
        # Provenance records a submission, not a result. These runs predate the
        # status write-back, so "completed" would be a fabrication.
        self.write_run("run-a", case_dir="/pscratch/work/tcv_unknown_result")
        self.adopt()
        self.assertEqual(
            sc.read_record("tcv_unknown_result", catalog_dir=self.catalog_dir)["status"],
            "submitted",
        )

    def test_recorded_states_are_honoured_when_present(self) -> None:
        self.write_run("run-a", case_dir="/pscratch/work/tcv_done_case", state="completed")
        self.adopt()
        self.assertEqual(
            sc.read_record("tcv_done_case", catalog_dir=self.catalog_dir)["status"],
            "completed",
        )

    def test_a_failed_run_marks_the_simulation_failed(self) -> None:
        self.write_run("run-a", case_dir="/pscratch/work/tcv_bad_case", state="completed")
        self.write_run("run-b", case_dir="/pscratch/work/tcv_bad_case", state="failed")
        self.adopt()
        self.assertEqual(
            sc.read_record("tcv_bad_case", catalog_dir=self.catalog_dir)["status"],
            "failed",
        )

    def test_adoption_is_idempotent(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_k1_case")
        self.adopt()
        again = self.adopt()
        self.assertEqual(again["written"], [])
        self.assertTrue(any("already adopted" in e["reason"] for e in again["skip"]))

    def test_overwrite_picks_up_newly_added_runs(self) -> None:
        self.write_run("run-a", case_dir="/a/tcv_k1_case")
        self.adopt()
        self.write_run("run-b", case_dir="/a/tcv_k1_case")

        self.adopt(overwrite=True)

        record = sc.read_record("tcv_k1_case", catalog_dir=self.catalog_dir)
        self.assertEqual(len(record["runs"]), 2)

    def test_adoption_never_overwrites_a_registered_simulation(self) -> None:
        sc.create_record(
            "tcv_k1_case",
            catalog_dir=self.catalog_dir,
            machines=["perlmutter"],
            short_name="real",
        )
        self.write_run("run-a", case_dir="/a/tcv_k1_case")

        result = self.adopt(overwrite=True)

        self.assertEqual(result["written"], [])
        self.assertTrue(any("already owns this key" in e["reason"] for e in result["skip"]))
        self.assertEqual(
            sc.read_record("tcv_k1_case", catalog_dir=self.catalog_dir)["origin"],
            "automation",
        )

    def test_legacy_top_level_records_are_adopted_too(self) -> None:
        path = self.runs / "old-run" / "provenance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": "old-run",
                    "machine": "stellar",
                    "case_dir": "/scratch/gpfs/x/ecbc_short_case",
                }
            ),
            encoding="utf-8",
        )
        self.adopt()
        record = sc.read_record("ecbc_short_case", catalog_dir=self.catalog_dir)
        self.assertEqual(record["adoption"]["provenance_classes"], ["legacy"])

    def test_missing_runs_directory_is_not_an_error(self) -> None:
        plan = run_adoption.plan_adoption(
            runs_dir=self.root / "nope", catalog_dir=self.catalog_dir
        )
        self.assertEqual(plan["counts"]["create"], 0)


class RealProvenanceAdoptionTests(unittest.TestCase):
    """Exercise adoption against the repository's actual provenance records."""

    def setUp(self) -> None:
        if not run_adoption.RUNS_DIR.is_dir():
            self.skipTest("no runs/ directory in this checkout")
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_every_orphan_run_lands_in_exactly_one_simulation(self) -> None:
        plan = run_adoption.plan_adoption(catalog_dir=self.catalog_dir)
        if not plan["orphan_runs"]:
            self.skipTest("no orphaned provenance records to adopt")

        adopted: list[str] = []
        for entry in plan["create"] + plan["update"]:
            adopted.extend(entry["run_ids"])

        self.assertEqual(len(adopted), len(set(adopted)), "a run was adopted twice")
        self.assertEqual(len(adopted), plan["counts"]["runs_adopted"])

    def test_derived_keys_are_all_valid_and_unique(self) -> None:
        plan = run_adoption.plan_adoption(catalog_dir=self.catalog_dir)
        ids = [entry["record_id"] for entry in plan["create"]]
        for record_id in ids:
            sc.validate_record_id(record_id)
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
