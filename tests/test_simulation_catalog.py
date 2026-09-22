from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from tools import simulation_catalog as sc


class RecordIdTests(unittest.TestCase):
    def test_accepts_modern_and_legacy_ids(self) -> None:
        self.assertEqual(sc.validate_record_id("n645pe"), "n645pe")
        self.assertEqual(sc.validate_record_id("N647PE_PO"), "n647pe_po")
        self.assertEqual(sc.validate_record_id("ti204"), "ti204")

    def test_rejects_path_traversal_and_junk(self) -> None:
        for bad in ("../etc/passwd", "n645pe/../x", "", "645", "n645pe.yaml"):
            with self.subTest(bad=bad):
                with self.assertRaises(sc.SimulationCatalogError):
                    sc.validate_record_id(bad)

    def test_modern_id_detection_separates_legacy_shots(self) -> None:
        self.assertTrue(sc.is_modern_id("n645pe"))
        self.assertTrue(sc.is_modern_id("n647pe_po"))
        self.assertTrue(sc.is_modern_id("d0001pe"))
        self.assertTrue(sc.is_modern_id("d0001pe_po"))
        self.assertFalse(sc.is_modern_id("ti204"))
        # Dev ids are four digits or more; a three-digit d-id is not a series.
        self.assertFalse(sc.is_modern_id("d001pe"))

    def test_number_extraction(self) -> None:
        self.assertEqual(sc.record_number("n645pe"), 645)
        self.assertEqual(sc.record_number("d0001pe"), 1)
        self.assertEqual(sc.record_number("ti204"), 204)

    def test_kind_comes_from_the_prefix_unless_the_record_says_otherwise(self) -> None:
        self.assertEqual(sc.record_kind("n645pe"), "production")
        self.assertEqual(sc.record_kind("d0001pe"), "dev")
        self.assertEqual(sc.record_kind("ti204"), "production")
        self.assertEqual(sc.record_kind({"record_id": "n662pe"}), "production")
        # A production-numbered run reclassified as a test keeps its number.
        self.assertEqual(sc.record_kind({"record_id": "n662pe", "kind": "dev"}), "dev")
        self.assertEqual(sc.new_record("d0001pe")["kind"], "dev")
        self.assertEqual(sc.new_record("n645pe")["kind"], "production")
        with self.assertRaises(sc.SimulationCatalogError):
            sc.new_record("n645pe", kind="test")

    def test_sub_tag_identity_preserves_directory_spelling(self) -> None:
        self.assertNotEqual(
            sc.identity_key("n645pe", "NT-1"),
            sc.identity_key("n645pe", "nt_1"),
        )


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create(self, record_id: str = "n645pe", **kwargs: object) -> dict:
        kwargs.setdefault("machines", ["perlmutter"])
        kwargs.setdefault("short_name", "EM KBM linear")
        return sc.create_record(record_id, catalog_dir=self.dir, **kwargs)

    def test_create_writes_one_file_per_simulation(self) -> None:
        record = self._create()
        path = self.dir / "n645pe.yaml"
        self.assertTrue(path.is_file())
        stored = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["record_id"], "n645pe")
        self.assertEqual(stored["number"], 645)
        self.assertEqual(stored["simulation_id"], "n645pe")
        self.assertEqual(stored["short_name"], "em_kbm_linear")
        self.assertEqual(stored["status"], "reserved")
        self.assertEqual(record["origin"], "automation")

    def test_create_refuses_to_overwrite_an_existing_simulation(self) -> None:
        self._create()
        with self.assertRaises(sc.SimulationCatalogError):
            self._create()

    def test_sub_tag_defaults_to_the_directory_basename(self) -> None:
        record = self._create(
            directory_name="/pscratch/sd/s/sku/n645pe_TCV_NT/NT1_psi_1.0"
        )
        self.assertEqual(record["simulation_tag"], "n645pe")
        self.assertEqual(record["sub_tag"], "NT1_psi_1.0")

    def test_same_simulation_tag_with_different_sub_tags_are_separate(self) -> None:
        first = self._create(
            simulation_tag="n645pe", sub_tag="baseline", directory_name="baseline"
        )
        second = self._create(
            "n645pe__reduced",
            simulation_tag="n645pe",
            sub_tag="reduced",
            directory_name="reduced",
        )
        self.assertEqual(first["simulation_tag"], second["simulation_tag"])
        self.assertNotEqual(first["sub_tag"], second["sub_tag"])
        found = sc.find_record_by_identity(
            "n645pe", "reduced", catalog_dir=self.dir
        )
        assert found is not None
        self.assertEqual(found["record_id"], "n645pe__reduced")

    def test_same_simulation_and_sub_tag_cannot_be_created_twice(self) -> None:
        self._create(simulation_tag="n645pe", sub_tag="same")
        with self.assertRaises(sc.SimulationCatalogError):
            self._create(
                "n645pe__duplicate", simulation_tag="n645pe", sub_tag="same"
            )

    def test_legacy_id_has_no_simulation_id(self) -> None:
        record = self._create("ti204", machines=[])
        self.assertIsNone(record["simulation_id"])
        self.assertEqual(record["number"], 204)

    def test_update_refuses_to_clobber_the_human_note(self) -> None:
        self._create()
        sc.set_note("n645pe", "human wrote this", catalog_dir=self.dir)
        with self.assertRaises(sc.SimulationCatalogError):
            sc.update_record("n645pe", {"note": "robot"}, catalog_dir=self.dir)
        self.assertEqual(
            sc.read_record("n645pe", catalog_dir=self.dir)["note"], "human wrote this"
        )

    def test_unknown_status_is_rejected(self) -> None:
        self._create()
        with self.assertRaises(sc.SimulationCatalogError):
            sc.update_record("n645pe", {"status": "vibing"}, catalog_dir=self.dir)

    def test_unknown_origin_is_rejected(self) -> None:
        # origin bounds what a record may claim, so it must not be free text.
        with self.assertRaises(sc.SimulationCatalogError):
            self._create("n700pe", origin="vibes")

    def test_adopted_directory_style_key_is_accepted(self) -> None:
        record = self._create(
            "tcv_xgca_reduce_adia_k1", machines=["perlmutter"], origin="adopted"
        )
        self.assertIsNone(record["number"])
        self.assertIsNone(record["simulation_id"])

    def test_attach_run_links_job_and_directory(self) -> None:
        self._create()
        record = sc.attach_run(
            "n645pe",
            {
                "run_id": "run-20260830T101010Z",
                "machine": "perlmutter",
                "job_id": "12345",
                "remote_run_dir": "/pscratch/x/n645pe_em_kbm_linear",
                "provenance": "runs/master/run-20260830T101010Z/provenance.json",
                "input_sha256": "abc123",
            },
            catalog_dir=self.dir,
        )
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(len(record["runs"]), 1)
        self.assertEqual(record["runs"][0]["job_id"], "12345")
        self.assertEqual(
            record["remote_dirs"]["perlmutter"], "/pscratch/x/n645pe_em_kbm_linear"
        )
        self.assertEqual(record["input_sha256"], "abc123")

    def test_attaching_the_same_run_twice_updates_in_place(self) -> None:
        self._create()
        run = {"run_id": "run-1", "machine": "perlmutter", "job_id": "1"}
        sc.attach_run("n645pe", run, catalog_dir=self.dir)
        record = sc.attach_run(
            "n645pe", {**run, "job_id": "2"}, catalog_dir=self.dir
        )
        self.assertEqual(len(record["runs"]), 1)
        self.assertEqual(record["runs"][0]["job_id"], "2")

    def test_attach_run_requires_a_run_id(self) -> None:
        self._create()
        with self.assertRaises(sc.SimulationCatalogError):
            sc.attach_run("n645pe", {"machine": "perlmutter"}, catalog_dir=self.dir)

    def test_terminal_state_advances_simulation_status(self) -> None:
        self._create()
        sc.attach_run(
            "n645pe", {"run_id": "run-1", "machine": "perlmutter"}, catalog_dir=self.dir
        )
        record = sc.update_run_state(
            "n645pe", "run-1", state="completed", exit_code=0, terminal=True,
            catalog_dir=self.dir,
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["runs"][0]["exit_code"], 0)

    def test_running_state_advances_from_submitted(self) -> None:
        self._create()
        sc.attach_run("n645pe", {"run_id": "run-1"}, catalog_dir=self.dir)
        record = sc.update_run_state(
            "n645pe", "run-1", state="running", catalog_dir=self.dir
        )
        self.assertEqual(record["status"], "running")

    def test_cancelled_run_does_not_change_simulation_status(self) -> None:
        self._create()
        sc.attach_run("n645pe", {"run_id": "run-1"}, catalog_dir=self.dir)
        record = sc.update_run_state(
            "n645pe", "run-1", state="cancelled", terminal=True, catalog_dir=self.dir
        )
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(record["runs"][0]["state"], "cancelled")

    def test_completed_run_does_not_drag_an_archived_simulation_backwards(self) -> None:
        self._create()
        sc.attach_run("n645pe", {"run_id": "run-1"}, catalog_dir=self.dir)
        sc.set_archive("n645pe", system="hpss", path="/home/s/sku/n645pe.tar", catalog_dir=self.dir)
        record = sc.update_run_state(
            "n645pe", "run-1", state="completed", terminal=True, catalog_dir=self.dir
        )
        self.assertEqual(record["status"], "archived")

    def test_update_run_state_rejects_an_unlinked_run(self) -> None:
        self._create()
        with self.assertRaises(sc.SimulationCatalogError):
            sc.update_run_state("n645pe", "run-nope", state="completed", catalog_dir=self.dir)

    def test_set_archive_records_location_and_status(self) -> None:
        self._create()
        record = sc.set_archive(
            "n645pe", system="hpss", path="/home/s/sku/n645pe.tar", catalog_dir=self.dir
        )
        self.assertEqual(record["status"], "archived")
        self.assertEqual(record["archive"]["system"], "hpss")
        self.assertEqual(record["archive"]["path"], "/home/s/sku/n645pe.tar")
        self.assertIsNotNone(record["archive"]["archived_at"])

    def test_read_record_raises_for_a_missing_simulation(self) -> None:
        with self.assertRaises(sc.SimulationCatalogError):
            sc.read_record("n999pe", catalog_dir=self.dir)

    def test_find_record_returns_none_for_a_missing_simulation(self) -> None:
        self.assertIsNone(sc.find_record("n999pe", catalog_dir=self.dir))

    def test_find_record_still_raises_on_a_corrupt_file(self) -> None:
        # Absence is normal; a damaged record is data loss and must be loud.
        (self.dir / "n700pe.yaml").write_text("just a string", encoding="utf-8")
        with self.assertRaises(sc.SimulationCatalogError):
            sc.find_record("n700pe", catalog_dir=self.dir)

    def test_iter_records_skips_a_corrupt_file_rather_than_failing(self) -> None:
        # Listing must survive one bad file, or a single corrupt record would
        # make the whole catalog unsearchable.
        self._create()
        (self.dir / "broken.yaml").write_text("{[not yaml", encoding="utf-8")
        ids = [record["record_id"] for record in sc.iter_records(catalog_dir=self.dir)]
        self.assertEqual(ids, ["n645pe"])


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        sc.create_record(
            "n645pe",
            catalog_dir=self.dir,
            machines=["perlmutter"],
            short_name="ASDEX I-mode electromagnetic",
            tags=["asdex", "electromagnetic"],
            status="completed",
            case="asdex-imode",
        )
        sc.create_record(
            "n646po",
            catalog_dir=self.dir,
            machines=["polaris"],
            short_name="ST40 heat load",
            tags=["st40"],
            status="running",
            case="st40-heatload",
        )
        legacy = sc.new_record(
            "ti204",
            short_name="old d3d core run",
            status="completed",
            origin="notebook",
        )
        legacy["started_on"] = "2014-06-01"
        legacy["description"] = "d3d core whole simulation with neoclassical transport"
        sc.write_record(legacy, catalog_dir=self.dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_query_returns_everything_sorted_by_number(self) -> None:
        rows = sc.search(catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["ti204", "n645pe", "n646po"])

    def test_dev_runs_sort_after_production_and_filter_by_kind(self) -> None:
        sc.create_record(
            "d0001pe",
            catalog_dir=self.dir,
            machines=["perlmutter"],
            short_name="g2 marker density",
            tags=["g2"],
        )
        rows = sc.search(catalog_dir=self.dir)
        # Number 1 would sort first if the two series were interleaved.
        self.assertEqual(
            [row["record_id"] for row in rows], ["ti204", "n645pe", "n646po", "d0001pe"]
        )
        self.assertEqual(
            [row["record_id"] for row in sc.search(kind="dev", catalog_dir=self.dir)],
            ["d0001pe"],
        )
        self.assertEqual(
            [row["record_id"] for row in sc.search(kind="production", catalog_dir=self.dir)],
            ["ti204", "n645pe", "n646po"],
        )
        self.assertEqual(rows[-1]["kind"], "dev")
        self.assertEqual(rows[0]["kind"], "production")
        self.assertEqual(sc.stats(catalog_dir=self.dir)["by_kind"], {"dev": 1, "production": 3})

    def test_free_text_matches_the_imported_description(self) -> None:
        rows = sc.search("neoclassical", catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["ti204"])

    def test_free_text_matches_the_short_name(self) -> None:
        rows = sc.search("asdex", catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["n645pe"])

    def test_separator_style_does_not_decide_findability(self) -> None:
        # The three catalog sources write names differently; a natural query
        # must find "ASDEX_I_mode_EM" regardless of how it is typed.
        for query in ("asdex i-mode", "ASDEX I mode", "asdex_i_mode", "Asdex  I-Mode"):
            with self.subTest(query=query):
                rows = sc.search(query, catalog_dir=self.dir)
                self.assertEqual([row["record_id"] for row in rows], ["n645pe"])

    def test_all_query_terms_must_match(self) -> None:
        self.assertEqual(sc.search("asdex", catalog_dir=self.dir)[0]["record_id"], "n645pe")
        # "asdex" matches, "st40" does not, so the record must not be returned.
        self.assertEqual(sc.search("asdex st40", catalog_dir=self.dir), [])

    def test_terms_may_match_across_different_fields(self) -> None:
        rows = sc.search("perlmutter asdex", catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["n645pe"])

    def test_machine_and_status_filters(self) -> None:
        self.assertEqual(
            [row["record_id"] for row in sc.search(machine="polaris", catalog_dir=self.dir)],
            ["n646po"],
        )
        self.assertEqual(
            [row["record_id"] for row in sc.search(status="running", catalog_dir=self.dir)],
            ["n646po"],
        )

    def test_origin_and_case_filters(self) -> None:
        self.assertEqual(
            [row["record_id"] for row in sc.search(origin="notebook", catalog_dir=self.dir)],
            ["ti204"],
        )
        self.assertEqual(
            [row["record_id"] for row in sc.search(case="st40-heatload", catalog_dir=self.dir)],
            ["n646po"],
        )

    def test_tag_filter_uses_normalized_tags(self) -> None:
        rows = sc.search(tag="electromagnetic", catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["n645pe"])

    def test_identity_filters_require_both_tags_when_both_are_given(self) -> None:
        rows = sc.search(
            simulation_tag="n645pe",
            sub_tag="n645pe",
            catalog_dir=self.dir,
        )
        self.assertEqual([row["record_id"] for row in rows], ["n645pe"])

    def test_date_range_filter_uses_started_on(self) -> None:
        rows = sc.search(before="2020-01-01", catalog_dir=self.dir)
        self.assertEqual([row["record_id"] for row in rows], ["ti204"])

    def test_limit_truncates(self) -> None:
        self.assertEqual(len(sc.search(limit=1, catalog_dir=self.dir)), 1)

    def test_stats_counts_by_dimension(self) -> None:
        payload = sc.stats(catalog_dir=self.dir)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["by_origin"], {"automation": 2, "notebook": 1})
        self.assertEqual(payload["by_status"]["completed"], 2)

    def test_summary_exposes_the_latest_run(self) -> None:
        sc.attach_run(
            "n645pe",
            {"run_id": "run-a", "machine": "perlmutter", "job_id": "1"},
            catalog_dir=self.dir,
        )
        sc.attach_run(
            "n645pe",
            {"run_id": "run-b", "machine": "perlmutter", "job_id": "2"},
            catalog_dir=self.dir,
        )
        summary = sc.summarize(sc.read_record("n645pe", catalog_dir=self.dir))
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["latest_run_id"], "run-b")
        self.assertEqual(summary["latest_job_id"], "2")

    def test_summary_exposes_parsed_input_for_sheet_sync(self) -> None:
        sc.attach_run(
            "n645pe",
            {
                "run_id": "run-input",
                "input": {
                    "parse_status": "ok",
                    "parameters": {
                        "sml_node_file": "mesh.node",
                        "sml_electron_on": True,
                        "eq_filename": "equilibrium.eqd",
                        "ptl_marker_type": ["ReducedDeltaF", "ReducedDeltaF"],
                    },
                    "derived": {
                        "field_model": "electromagnetic",
                        "marker_model": "reduced-delta-f",
                        "species": ["e", "i"],
                    },
                },
            },
            catalog_dir=self.dir,
        )

        summary = sc.summarize(sc.read_record("n645pe", catalog_dir=self.dir))
        self.assertEqual(summary["field_model"], "electromagnetic")
        self.assertEqual(summary["marker_model"], "reduced-delta-f")
        self.assertEqual(summary["species"], ["e", "i"])
        self.assertEqual(summary["sml_node_file"], "mesh.node")
        self.assertEqual(summary["eq_filename"], "equilibrium.eqd")


if __name__ == "__main__":
    unittest.main()


class DeriveStatusResubmissionTest(unittest.TestCase):
    def test_pending_resubmission_after_failure_is_in_flight(self):
        record = {
            "status": "failed",
            "runs": [
                {"run_id": "run-20260915T025441Z", "state": "failed", "exit_code": 15,
                 "submitted_at": "2026-09-15T02:55:39+00:00"},
                {"run_id": "run-20260916T011916Z", "state": "pending",
                 "submitted_at": "2026-09-16T01:20:16+00:00"},
            ],
        }
        self.assertEqual(sc.derive_status(record), "submitted")

    def test_pending_restart_after_timeout_is_in_flight(self):
        record = {
            "status": "interrupted",
            "runs": [
                {"run_id": "run-1", "state": "timeout", "submitted_at": "2026-09-10T00:00:00+00:00"},
                {"run_id": "run-2", "state": "pending", "submitted_at": "2026-09-11T00:00:00+00:00"},
            ],
        }
        self.assertEqual(sc.derive_status(record), "submitted")

    def test_outcome_after_pending_history_still_wins(self):
        record = {
            "status": "submitted",
            "runs": [
                {"run_id": "run-1", "state": "pending", "submitted_at": "2026-09-10T00:00:00+00:00"},
                {"run_id": "run-2", "state": "failed", "exit_code": 1,
                 "submitted_at": "2026-09-11T00:00:00+00:00"},
            ],
        }
        self.assertEqual(sc.derive_status(record), "failed")

    def test_human_judgement_is_not_overridden_by_pending_run(self):
        record = {"status": "abandoned",
                  "runs": [{"run_id": "run-1", "state": "pending", "submitted_at": "2026-09-11T00:00:00+00:00"}]}
        self.assertEqual(sc.derive_status(record), "abandoned")
