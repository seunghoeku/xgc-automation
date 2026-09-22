from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import input_archive as archive


class InputArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive_dir = self.root / "input_backup"
        self.cache_dir = self.root / ".cache"
        self.archive_dir.mkdir()
        self.cache_dir.mkdir()
        self.metadata_path = self.cache_dir / "metadata-stellar-test.json"
        self.metadata_path.write_text(
            json.dumps(
                {
                    "sml_param": {
                        "parameters": {
                            "sml_dt": {},
                            "sml_mstep": {},
                            "sml_restart": {},
                        }
                    },
                    "col_param": {"parameters": {"col_mode": {}}},
                }
            ),
            encoding="utf-8",
        )
        (self.archive_dir / "n623fr_ITER_ES").write_text(
            """&sml_param
sml_dt=2.0e-4
sml_mstep=100
sml_restart=.false.
old_removed_parameter=1
/
&col_param
col_mode=0
/
""",
            encoding="utf-8",
        )
        (self.archive_dir / "n623fr_ITER_ES_variant").write_text(
            "&sml_param\nsml_dt=1.0e-4\nsml_mstep=50\n/\n",
            encoding="utf-8",
        )
        (self.archive_dir / "n585pe_old").write_text(
            "&sml_param\nsml_dt=1.0e-4\n/\n",
            encoding="utf-8",
        )
        (self.archive_dir / "d0623pe_g2_test").write_text(
            "&sml_param\nsml_dt=1.0e-4\n/\n",
            encoding="utf-8",
        )

        self.archive_patch = patch.object(archive, "ARCHIVE_DIR", self.archive_dir)
        self.cache_patch = patch.object(archive, "CACHE_DIR", self.cache_dir)
        self.archive_patch.start()
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.archive_patch.stop()
        self.tmp.cleanup()

    def test_recent_selection_and_schema_comparison(self) -> None:
        result = archive.inspect_archive(min_number=600)

        self.assertEqual(result["selection"]["returned"], 2)
        self.assertEqual(result["counts"]["distinct_selected_numbers"], 1)
        self.assertIn("623", result["duplicate_number_variants"])
        self.assertTrue(all(item["kind"] == "production" for item in result["records"]))

    def test_dev_inputs_are_a_separate_series(self) -> None:
        # Left out by default: a dev number must never be joined to a notebook
        # row, and d0623 is not the same simulation as n623.
        default = archive.inspect_archive(min_number=600)
        self.assertNotIn(
            "d0623pe_g2_test", [item["archive_name"] for item in default["records"]]
        )
        result = archive.inspect_archive(min_number=600, include_dev=True)
        dev = [item for item in result["records"] if item["kind"] == "dev"]
        self.assertEqual([item["archive_name"] for item in dev], ["d0623pe_g2_test"])
        self.assertEqual(dev[0]["simulation_number"], 623)
        self.assertEqual(dev[0]["machines"], ["perlmutter"])
        self.assertEqual(sorted(result["duplicate_number_variants"]), ["623"])
        self.assertEqual(result["counts"]["distinct_selected_numbers"], 2)
        record = next(
            item
            for item in result["records"]
            if item["archive_name"] == "n623fr_ITER_ES"
        )
        self.assertEqual(record["machines"], ["frontier"])
        self.assertEqual(
            record["schema_comparison"]["unknown_parameters"],
            ["old_removed_parameter"],
        )
        self.assertEqual(record["key_parameters"]["sml_mstep"], "100")
        self.assertEqual(record["validation_status"], "unvalidated_archive")

    def test_older_input_is_excluded_by_default_threshold(self) -> None:
        result = archive.inspect_archive()
        names = {item["archive_name"] for item in result["records"]}

        self.assertNotIn("n585pe_old", names)

    def test_comment_marker_inside_string_is_preserved(self) -> None:
        parsed = archive.parse_input_text(
            "&sml_param\nname='value!still-string' ! real comment\n/\n"
        )
        self.assertEqual(parsed["parameters"]["name"], "'value!still-string'")

    def test_archive_is_not_modified(self) -> None:
        path = self.archive_dir / "n623fr_ITER_ES"
        before = path.read_bytes()
        archive.inspect_archive(min_number=600)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
