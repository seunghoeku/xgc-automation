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


if __name__ == "__main__":
    unittest.main()
