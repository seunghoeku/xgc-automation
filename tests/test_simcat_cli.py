from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import simcat_cli
from tools import simulation_catalog as sc


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        sc.create_record(
            "n645pe",
            catalog_dir=self.dir,
            machines=["perlmutter"],
            short_name="ASDEX I-mode electromagnetic",
            status="completed",
            case="asdex-imode",
            tags=["asdex"],
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

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = simcat_cli.main(["--catalog-dir", str(self.dir), *argv])
        return code, buffer.getvalue()

    def run_cli_json(self, *argv: str) -> tuple[int, object]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = simcat_cli.main(["--json", "--catalog-dir", str(self.dir), *argv])
        return code, json.loads(buffer.getvalue())

    def test_list_prints_a_table(self) -> None:
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("n645pe", out)
        self.assertIn("n646po", out)
        self.assertIn("2 simulation(s)", out)

    def test_json_output_is_machine_readable(self) -> None:
        code, payload = self.run_cli_json("list")
        self.assertEqual(code, 0)
        self.assertEqual([row["record_id"] for row in payload], ["n645pe", "n646po"])

    def test_json_works_before_or_after_the_subcommand(self) -> None:
        # `xgc-sim list --json` is the obvious thing to type; accepting it only
        # in front of the subcommand is a trap.
        for argv in (
            ["--json", "--catalog-dir", str(self.dir), "list"],
            ["list", "--json", "--catalog-dir", str(self.dir)],
            ["--json", "list", "--catalog-dir", str(self.dir)],
        ):
            with self.subTest(argv=argv):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    self.assertEqual(simcat_cli.main(argv), 0)
                payload = json.loads(buffer.getvalue())
                self.assertEqual(len(payload), 2)

    def test_table_output_remains_the_default(self) -> None:
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("2 simulation(s)", out)

    def test_search_matches_free_text(self) -> None:
        _, payload = self.run_cli_json("search", "asdex")
        self.assertEqual([row["record_id"] for row in payload], ["n645pe"])

    def test_filters_compose(self) -> None:
        _, payload = self.run_cli_json("list", "--machine", "polaris", "--status", "running")
        self.assertEqual([row["record_id"] for row in payload], ["n646po"])

    def test_show_renders_one_simulation(self) -> None:
        code, out = self.run_cli("show", "n645pe")
        self.assertEqual(code, 0)
        self.assertIn("n645pe", out)
        self.assertIn("completed", out)
        self.assertIn("no scheduler runs linked yet", out)

    def test_note_writes_the_human_field(self) -> None:
        code, _ = self.run_cli("note", "n645pe", "matches the GENE benchmark")
        self.assertEqual(code, 0)
        self.assertEqual(
            sc.read_record("n645pe", catalog_dir=self.dir)["note"],
            "matches the GENE benchmark",
        )

    def test_status_updates_the_lifecycle(self) -> None:
        self.run_cli("status", "n646po", "completed")
        self.assertEqual(
            sc.read_record("n646po", catalog_dir=self.dir)["status"], "completed"
        )

    def test_archive_records_where_output_went(self) -> None:
        self.run_cli("archive", "n645pe", "--system", "hpss", "--path", "/home/s/sku/n645pe.tar")
        record = sc.read_record("n645pe", catalog_dir=self.dir)
        self.assertEqual(record["status"], "archived")
        self.assertEqual(record["archive"]["path"], "/home/s/sku/n645pe.tar")

    def test_stats_reports_totals(self) -> None:
        _, payload = self.run_cli_json("stats")
        self.assertEqual(payload["total"], 2)

    def test_unknown_simulation_exits_with_an_error(self) -> None:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            code = simcat_cli.main(
                ["--catalog-dir", str(self.dir), "show", "n999pe"]
            )
        self.assertEqual(code, 2)
        self.assertIn("xgc-sim:", buffer.getvalue())

    def test_invalid_status_is_rejected_by_the_parser(self) -> None:
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                simcat_cli.main(
                    ["--catalog-dir", str(self.dir), "status", "n645pe", "vibing"]
                )

    def test_import_without_confirm_reports_but_writes_nothing(self) -> None:
        from tools import case_promotion as cp

        notes = [cp.SimulationNote(shot="ti204", number=204, sheet="y2014")]
        # Exercise the same path the CLI uses, with an in-memory notebook.
        from tools import legacy_import

        plan = legacy_import.plan_import(notes=notes, catalog_dir=self.dir)
        self.assertEqual(plan["counts"]["create"], 1)
        self.assertFalse((self.dir / "ti204.yaml").exists())


if __name__ == "__main__":
    unittest.main()
