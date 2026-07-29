from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tools import simulation_registry as sr


class SimulationRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / "simulation_registry.yaml"
        self.registry_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "historical_through": 644,
                    "next_number": 645,
                    "machine_codes": {
                        "perlmutter": "pe",
                        "polaris": "po",
                        "aurora": "au",
                        "doudna": "do",
                        "stellar": "st",
                    },
                    "machine_aliases": {"douna": "doudna"},
                    "reservations": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.path_patch = patch.object(sr, "REGISTRY_PATH", self.registry_path)
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_plan_uses_global_next_number_and_removes_machine_name(self) -> None:
        plan = sr.plan_simulation_id(
            "aurora",
            "Aurora EM KBM linear",
            tags=["Aurora", "au", "electromagnetic", "linear"],
        )

        self.assertEqual(plan["simulation_id"], "n645au")
        self.assertEqual(plan["directory_name"], "n645au_em_kbm_linear")
        self.assertEqual(plan["machines"], ["aurora"])
        self.assertEqual(plan["tags"], ["electromagnetic", "linear"])
        self.assertFalse(plan["writes"])

    def test_multi_machine_id_preserves_primary_machine_order(self) -> None:
        plan = sr.plan_simulation_id(
            ["perlmutter", "polaris"],
            "TCV benchmark",
        )

        self.assertEqual(plan["simulation_id"], "n645pe_po")
        self.assertEqual(plan["directory_name"], "n645pe_po_tcv_benchmark")

    def test_official_doudna_alias_is_normalized(self) -> None:
        plan = sr.plan_simulation_id("douna", "Doudna porting")

        self.assertEqual(plan["simulation_id"], "n645do")
        self.assertEqual(plan["machines"], ["doudna"])
        self.assertEqual(plan["directory_name"], "n645do_porting")

    def test_reservation_requires_confirmation_without_writing(self) -> None:
        result = sr.reserve_simulation_id("aurora", "EM test")
        data = yaml.safe_load(self.registry_path.read_text(encoding="utf-8"))

        self.assertFalse(result["reserved"])
        self.assertEqual(data["next_number"], 645)
        self.assertEqual(data["reservations"], [])

    def test_confirmed_reservation_increments_global_number(self) -> None:
        first = sr.reserve_simulation_id(
            "aurora",
            "EM test",
            expected_number=645,
            confirm=True,
        )
        second_plan = sr.plan_simulation_id("doudna", "porting")

        self.assertTrue(first["reserved"])
        self.assertEqual(first["simulation_id"], "n645au")
        self.assertEqual(second_plan["simulation_id"], "n646do")

    def test_stale_plan_is_rejected(self) -> None:
        sr.reserve_simulation_id(
            "aurora",
            "EM test",
            expected_number=645,
            confirm=True,
        )
        with self.assertRaisesRegex(sr.SimulationRegistryError, "stale"):
            sr.reserve_simulation_id(
                "doudna",
                "porting",
                expected_number=645,
                confirm=True,
            )

    def test_directory_uses_machine_run_root(self) -> None:
        sr.reserve_simulation_id(
            "stellar",
            "TCV delta-f",
            expected_number=645,
            confirm=True,
        )
        config = {"paths": {"run_root": "/scratch/xgc-runs"}}
        with patch.object(sr.machine_config, "get_machine", return_value=config):
            result = sr.simulation_directory("n645st")

        self.assertEqual(
            result["remote_directory"],
            "/scratch/xgc-runs/n645st_tcv_delta_f",
        )

    def test_stage_case_uses_canonical_directory(self) -> None:
        sr.reserve_simulation_id(
            "stellar",
            "TCV delta-f",
            expected_number=645,
            confirm=True,
        )
        config = {"paths": {"run_root": "/scratch/xgc-runs"}}
        with (
            patch.object(sr.machine_config, "get_machine", return_value=config),
            patch.object(
                sr.input_builder,
                "stage_case",
                return_value={"staged": False, "reason": "confirmation required"},
            ) as stage,
        ):
            result = sr.stage_simulation_case("ecbc", "n645st")

        stage.assert_called_once_with(
            "ecbc",
            "/scratch/xgc-runs/n645st_tcv_delta_f",
            confirm=False,
            machine="stellar",
        )
        self.assertEqual(result["simulation_id"], "n645st")

    def test_same_machine_cannot_be_listed_twice(self) -> None:
        with self.assertRaisesRegex(sr.SimulationRegistryError, "more than once"):
            sr.plan_simulation_id(["aurora", "aurora"], "EM test")


if __name__ == "__main__":
    unittest.main()
