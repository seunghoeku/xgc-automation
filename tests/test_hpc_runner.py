from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import hpc_runner as hr


class OutputRunDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {"paths": {"run_root": "/scratch/runs"}}

    def test_uses_recorded_case_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            provenance_dir = runs_dir / "run-1"
            provenance_dir.mkdir()
            (provenance_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "machine": "stellar",
                        "remote_run_dir": "/scratch/cases/ecbc-short",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(hr, "RUNS_DIR", runs_dir):
                resolved = hr._output_run_dir("stellar", "run-1", self.cfg)

        self.assertEqual(resolved, "/scratch/cases/ecbc-short")

    def test_falls_back_to_run_root_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "RUNS_DIR", Path(tmp)):
                resolved = hr._output_run_dir("stellar", "run-2", self.cfg)

        self.assertEqual(resolved, "/scratch/runs/run-2")

    def test_rejects_machine_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            provenance_dir = runs_dir / "run-3"
            provenance_dir.mkdir()
            (provenance_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "machine": "polaris",
                        "remote_run_dir": "/eagle/project/run-3",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(hr, "RUNS_DIR", runs_dir):
                with self.assertRaisesRegex(hr.SubmissionError, "belongs to"):
                    hr._output_run_dir("stellar", "run-3", self.cfg)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(hr.SubmissionError, "invalid run_id"):
            hr._output_run_dir("stellar", "../run-4", self.cfg)


class SimulationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "slurm",
                "submit_tpl": "templates/slurm.sbatch.j2",
            },
            "paths": {"run_root": "/scratch/xgc-runs"},
            "xgc": {"exe": "/opt/xgc/bin/xgc", "modules": []},
            "defaults": {
                "account": "project",
                "partition": "all",
                "nodes": 1,
                "walltime": "00:10:00",
            },
        }
        self.record = {
            "simulation_id": "n645st",
            "simulation_number": 645,
            "machines": ["stellar"],
            "machine_codes": ["st"],
            "short_name": "tcv_delta_f",
            "directory_name": "n645st_tcv_delta_f",
            "tags": ["electrostatic"],
        }
        self.directory = {
            "simulation_id": "n645st",
            "machine": "stellar",
            "directory_name": "n645st_tcv_delta_f",
            "remote_directory": "/scratch/xgc-runs/n645st_tcv_delta_f",
            "tags": ["electrostatic"],
        }

    def test_reserved_simulation_uses_canonical_directory(self) -> None:
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(
                hr.sim_registry,
                "simulation_directory",
                return_value=self.directory,
            ),
            patch.object(
                hr.sim_registry,
                "get_simulation",
                return_value=self.record,
            ),
        ):
            plan = hr.prepare_submission(
                "stellar",
                simulation_id="n645st",
                ntasks_per_node=8,
            )

        self.assertEqual(
            plan.remote_run_dir,
            "/scratch/xgc-runs/n645st_tcv_delta_f",
        )
        self.assertEqual(plan.job_name, "xgc-n645st")
        self.assertEqual(plan.simulation["simulation_number"], 645)
        self.assertIn(
            "#SBATCH --output=/scratch/xgc-runs/n645st_tcv_delta_f/slurm-%j.out",
            plan.script,
        )

    def test_noncanonical_case_directory_is_rejected(self) -> None:
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(
                hr.sim_registry,
                "simulation_directory",
                return_value=self.directory,
            ),
            patch.object(
                hr.sim_registry,
                "get_simulation",
                return_value=self.record,
            ),
        ):
            with self.assertRaisesRegex(hr.SubmissionError, "canonical"):
                hr.prepare_submission(
                    "stellar",
                    simulation_id="n645st",
                    run_dir="/scratch/xgc-runs/wrong-name",
                )

    def test_simulation_id_is_rejected_for_hello_world(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaisesRegex(hr.SubmissionError, "hello-world"):
                hr.prepare_submission(
                    "stellar",
                    simulation_id="n645st",
                    hello_world=True,
                )


if __name__ == "__main__":
    unittest.main()
