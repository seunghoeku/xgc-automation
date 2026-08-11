from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import data_analyzer as da
from tools.ssh import CommandResult


def _inventory(remote: str = "/scratch/run-1") -> dict:
    return {
        "schema": "xgc-run-inventory/v1",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "remote_run_dir": remote,
        "scan": {
            "scope": "top-level-only",
            "entry_limit": 1000,
            "entry_count_total": 2,
            "entry_count_returned": 2,
            "truncated": False,
        },
        "counts": {
            "adios2_products": 1,
            "regular_files": 1,
            "directories": 0,
            "symlinks": 0,
            "other": 0,
        },
        "top_level_regular_file_bytes": 42,
        "entries": [
            {
                "name": "xgc.3d.00001.bp",
                "kind": "adios2_product",
                "size_bytes": None,
                "mtime": "2026-07-29T00:00:00+00:00",
            },
            {
                "name": "units.txt",
                "kind": "file",
                "size_bytes": 42,
                "mtime": "2026-07-29T00:00:00+00:00",
            },
        ],
        "warnings": ["ADIOS2 .bp contents were not read or transferred."],
    }


def _oned_profiles(remote: str = "/scratch/run-1") -> dict:
    return {
        "schema": "xgc-oned-profiles/v1",
        "generated_at": "2026-07-29T00:00:00+00:00",
        "remote_run_dir": remote,
        "source_product": "xgc.oneddiag.bp",
        "reader": {
            "package": "xgc-analysis",
            "version": "0.1-dev",
            "module_path": "/opt/xgc-analysis/xgc_analysis/__init__.py",
            "git_commit": "252c3ea",
            "catalog_mode": "selected-product-metadata",
            "track_state": False,
        },
        "selection": {
            "policy": "first-and-latest-logical-step",
            "available_step_count": 11,
            "selected_logical_steps": [0, 10],
            "max_profile_points": 512,
            "source_radial_point_count": 4,
            "returned_radial_point_count": 4,
            "estimated_values_upper_bound": 64,
        },
        "coordinate": {
            "variable": "psi",
            "logical_step": 0,
            "values": [0.0, 0.3, 0.7, 1.0],
        },
        "step_metadata": {
            "0": {"logical_step": 0, "time": 0.0, "gstep": 0},
            "10": {"logical_step": 10, "time": 0.1, "gstep": 10},
        },
        "profiles": [
            {
                "variable": "e_gc_density_df_1d",
                "species": "e",
                "quantity": "gc_density_df_1d",
                "unit": "m^-3",
                "values_by_logical_step": {
                    "0": [1.0, 2.0, 3.0, 4.0],
                    "10": [1.5, 2.5, 3.5, 4.5],
                },
            },
            {
                "variable": "e_perp_temperature_df_1d",
                "species": "e",
                "quantity": "perp_temperature_df_1d",
                "unit": "eV",
                "values_by_logical_step": {
                    "0": [100.0, 90.0, 70.0, 20.0],
                    "10": [102.0, 91.0, 71.0, 22.0],
                },
            },
        ],
        "warnings": [],
    }


class DataAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name)
        self.run_dir = self.runs_dir / "run-1"
        self.run_dir.mkdir()
        (self.run_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "machine": "stellar",
                    "remote_run_dir": "/scratch/run-1",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _remote_result(self, payload: dict | None = None) -> CommandResult:
        return CommandResult(
            returncode=0,
            stdout=json.dumps(payload or _inventory()),
            stderr="",
            command="python3 -c ...",
        )

    def test_inspect_returns_bounded_inventory(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(da.ssh, "run", return_value=self._remote_result()) as run,
        ):
            result = da.inspect_run("stellar", "run-1")

        self.assertEqual(result["schema"], "xgc-run-inventory/v1")
        self.assertEqual(result["counts"]["adios2_products"], 1)
        self.assertEqual(result["machine"], "stellar")
        remote_argv = run.call_args.args[1]
        self.assertEqual(remote_argv[0:2], ["python3", "-c"])
        self.assertIn("/scratch/run-1", remote_argv)

    def test_classified_provenance_is_resolved(self) -> None:
        classified_dir = self.runs_dir / "master" / "run-master"
        classified_dir.mkdir(parents=True)
        (classified_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "machine": "stellar",
                    "remote_run_dir": "/scratch/run-master",
                    "provenance_class": "master",
                }
            ),
            encoding="utf-8",
        )

        with patch.object(da, "RUNS_DIR", self.runs_dir):
            run_dir, record = da._run_record("run-master")

        self.assertEqual(run_dir, classified_dir)
        self.assertEqual(record["provenance_class"], "master")

    def test_analyze_saves_only_json_summary(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(da.ssh, "run", return_value=self._remote_result()),
        ):
            result = da.analyze_run("stellar", "run-1")

        output = Path(result["output_path"])
        self.assertTrue(result["saved"])
        self.assertEqual(output.name, "inventory.json")
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["data"]["entries"][0]["kind"], "adios2_product")
        self.assertFalse(any(path.suffix == ".bp" for path in output.parent.iterdir()))

    def test_existing_summary_requires_overwrite_confirmation(self) -> None:
        summary_dir = self.run_dir / "summaries"
        summary_dir.mkdir()
        output = summary_dir / "inventory.json"
        output.write_text('{"old": true}\n', encoding="utf-8")

        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(da.ssh, "run", return_value=self._remote_result()),
        ):
            result = da.analyze_run("stellar", "run-1")

        self.assertFalse(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(output.read_text(encoding="utf-8"), '{"old": true}\n')

    def test_machine_mismatch_is_rejected_before_ssh(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.ssh, "run") as run,
        ):
            with self.assertRaisesRegex(da.DataAnalysisError, "belongs to"):
                da.inspect_run("polaris", "run-1")
        run.assert_not_called()

    def test_path_traversal_run_id_is_rejected(self) -> None:
        with patch.object(da, "RUNS_DIR", self.runs_dir):
            with self.assertRaisesRegex(da.DataAnalysisError, "invalid run_id"):
                da.inspect_run("stellar", "../run-1")

    def test_invalid_remote_schema_is_rejected(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(
                da.ssh,
                "run",
                return_value=self._remote_result({"schema": "unknown/v1"}),
            ),
        ):
            with self.assertRaisesRegex(da.DataAnalysisError, "unsupported schema"):
                da.inspect_run("stellar", "run-1")

    def test_remote_reducer_does_not_descend_into_bp_directory(self) -> None:
        fixture = self.runs_dir / "remote-fixture"
        product = fixture / "xgc.3d.00001.bp"
        product.mkdir(parents=True)
        (product / "large-hidden-array").write_bytes(b"x" * 10_000)
        (fixture / "units.txt").write_text("units\n", encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, "-c", da._REMOTE_INVENTORY, str(fixture), "1000"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)

        self.assertEqual(payload["scan"]["scope"], "top-level-only")
        self.assertEqual(payload["counts"]["adios2_products"], 1)
        self.assertNotIn("large-hidden-array", proc.stdout)

    def test_oned_profiles_runs_xgc_analysis_reducer_remotely(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(
                da.m,
                "get_machine",
                return_value={
                    "auth": {},
                    "analysis": {
                        "python": "/opt/analysis/bin/python",
                        "pythonpath": "/opt/XGC-Analysis",
                        "env_script": "/opt/xgc-env.sh",
                    },
                },
            ),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(
                da.ssh,
                "run",
                return_value=self._remote_result(_oned_profiles()),
            ) as run,
        ):
            result = da.inspect_oned_profiles("stellar", "run-1", max_points=128)

        self.assertEqual(result["schema"], "xgc-oned-profiles/v1")
        self.assertEqual(result["machine"], "stellar")
        self.assertEqual(result["profiles"][0]["species"], "e")
        remote_argv = run.call_args.args[1]
        self.assertEqual(remote_argv[0:2], ["env", "PYTHONPATH=/opt/XGC-Analysis"])
        self.assertEqual(remote_argv[2:4], ["/opt/analysis/bin/python", "-c"])
        self.assertEqual(remote_argv[-2:], ["/scratch/run-1", "128"])
        self.assertEqual(run.call_args.kwargs["env_script"], "/opt/xgc-env.sh")

    def test_oned_profiles_saves_json_and_local_svg(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(
                da.ssh,
                "run",
                return_value=self._remote_result(_oned_profiles()),
            ),
        ):
            result = da.analyze_run("stellar", "run-1", product="oned_profiles")

        summary_path = Path(result["output_path"])
        plot_path = Path(result["plot_path"])
        self.assertTrue(summary_path.is_file())
        self.assertTrue(plot_path.is_file())
        self.assertEqual(
            json.loads(summary_path.read_text(encoding="utf-8"))["data"]["schema"],
            "xgc-oned-profiles/v1",
        )
        svg = plot_path.read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("Guiding-center density", svg)
        self.assertNotIn(".bp", plot_path.name)

    def test_existing_oned_plot_requires_overwrite_confirmation(self) -> None:
        summary_dir = self.run_dir / "summaries"
        summary_dir.mkdir()
        plot_path = summary_dir / "oned_profiles.svg"
        plot_path.write_text("<svg>old</svg>\n", encoding="utf-8")

        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.m, "get_machine", return_value={"auth": {}}),
            patch.object(da.ssh, "host_for", return_value="stellar"),
            patch.object(
                da.ssh,
                "run",
                return_value=self._remote_result(_oned_profiles()),
            ),
        ):
            result = da.analyze_run("stellar", "run-1", product="oned_profiles")

        self.assertFalse(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(plot_path.read_text(encoding="utf-8"), "<svg>old</svg>\n")
        self.assertFalse((summary_dir / "oned_profiles.json").exists())

    def test_oned_profiles_rejects_unbounded_point_request_before_ssh(self) -> None:
        with (
            patch.object(da, "RUNS_DIR", self.runs_dir),
            patch.object(da.ssh, "run") as run,
        ):
            with self.assertRaisesRegex(da.DataAnalysisError, "between 16 and 2048"):
                da.inspect_oned_profiles("stellar", "run-1", max_points=10_000)
        run.assert_not_called()

    def test_oned_reducer_uses_fast_catalog_and_selected_steps(self) -> None:
        script = da._REMOTE_ONED_PROFILES
        self.assertIn("metadata_product_keys={PRODUCT}", script)
        self.assertIn("track_state=False", script)
        self.assertIn("selected_steps", script)
        self.assertIn("catalog.read_arrays", script)
        self.assertNotIn("ssh.pull", script)


if __name__ == "__main__":
    unittest.main()
