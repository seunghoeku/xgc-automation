from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tools import machines as m


BASE = {
    "polaris": {
        "host": "polaris.alcf.anl.gov",
        "user": "<alcf_username>",
        "auth": {"method": "ssh_tunnel", "ssh_config_host": "<ssh_alias>"},
        "scheduler": {"type": "pbs", "queue_shapes": [1, 10]},
        "xgc": {"exe": None, "gpu_aware_mpi": False, "modules": ["a", "b"]},
        "defaults": {"account": "<alcf_project>", "queue": "debug-scaling"},
    },
    "common": {"data": {"pull_only_summaries": True}},
}

LOCAL = {
    "polaris": {
        "user": "alice",
        "auth": {"ssh_config_host": "polaris2"},
        "xgc": {"exe": "/grand/Proj/alice/xgc", "modules": ["c"]},
        "defaults": {"account": "Proj"},
    },
}


class MachinesOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base_file = root / "machines.yaml"
        self.local_file = root / "machines.local.yaml"
        self.base_file.write_text(yaml.safe_dump(BASE), encoding="utf-8")
        self.patches = [
            patch.object(m, "MACHINES_FILE", self.base_file),
            patch.object(m, "LOCAL_MACHINES_FILE", self.local_file),
        ]
        for item in self.patches:
            item.start()
        m.load_machines.cache_clear()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        m.load_machines.cache_clear()
        self.tmp.cleanup()

    def test_without_local_file_placeholders_remain(self) -> None:
        cfg = m.get_machine("polaris")
        self.assertTrue(m.is_placeholder(cfg["user"]))
        self.assertTrue(m.is_placeholder(cfg["auth"]["ssh_config_host"]))
        # Universal facts are present regardless.
        self.assertEqual(cfg["scheduler"]["type"], "pbs")

    def test_local_overlay_merges_deeply_and_replaces_lists(self) -> None:
        self.local_file.write_text(yaml.safe_dump(LOCAL), encoding="utf-8")
        m.load_machines.cache_clear()
        cfg = m.get_machine("polaris")

        # Overridden values.
        self.assertEqual(cfg["user"], "alice")
        self.assertEqual(cfg["auth"]["ssh_config_host"], "polaris2")
        self.assertEqual(cfg["xgc"]["exe"], "/grand/Proj/alice/xgc")
        self.assertEqual(cfg["defaults"]["account"], "Proj")
        # Sibling committed values survive the merge.
        self.assertEqual(cfg["auth"]["method"], "ssh_tunnel")
        self.assertIs(cfg["xgc"]["gpu_aware_mpi"], False)
        self.assertEqual(cfg["defaults"]["queue"], "debug-scaling")
        self.assertEqual(cfg["scheduler"]["queue_shapes"], [1, 10])
        # Lists replace, not concatenate.
        self.assertEqual(cfg["xgc"]["modules"], ["c"])

    def test_unknown_machine_in_local_file_is_rejected(self) -> None:
        bad = {"mystery": {"user": "alice"}}
        self.local_file.write_text(yaml.safe_dump(bad), encoding="utf-8")
        m.load_machines.cache_clear()
        with self.assertRaises(ValueError):
            m.load_machines()

    def test_base_file_is_not_modified_by_merge(self) -> None:
        before = self.base_file.read_bytes()
        self.local_file.write_text(yaml.safe_dump(LOCAL), encoding="utf-8")
        m.load_machines.cache_clear()
        m.load_machines()
        self.assertEqual(self.base_file.read_bytes(), before)


class CaseLocalOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        from tools import input_builder as ib

        self.ib = ib
        self.tmp = tempfile.TemporaryDirectory()
        self.cases_dir = Path(self.tmp.name)
        (self.cases_dir / "demo.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "demo",
                    "machine": "polaris",
                    "case_dir": "/grand/Original/owner/demo",
                    "physics": {"model": "electrostatic"},
                }
            ),
            encoding="utf-8",
        )
        self.patch = patch.object(ib, "CASES_DIR", self.cases_dir)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def test_local_override_rebinds_site_fields_only(self) -> None:
        (self.cases_dir / "demo.local.yaml").write_text(
            yaml.safe_dump({"case_dir": "/grand/Proj/alice/demo-restaged"}),
            encoding="utf-8",
        )
        case = self.ib.load_case("demo")
        self.assertEqual(case["case_dir"], "/grand/Proj/alice/demo-restaged")
        self.assertEqual(case["physics"]["model"], "electrostatic")
        self.assertIn("local_override", case)

    def test_local_override_rejects_physics_changes(self) -> None:
        (self.cases_dir / "demo.local.yaml").write_text(
            yaml.safe_dump({"physics": {"model": "electromagnetic"}}),
            encoding="utf-8",
        )
        with self.assertRaises(self.ib.InputBuildError):
            self.ib.load_case("demo")

    def test_local_files_hidden_from_list_cases(self) -> None:
        (self.cases_dir / "demo.local.yaml").write_text("{}", encoding="utf-8")
        names = [c["name"] for c in self.ib.list_cases()]
        self.assertEqual(names, ["demo"])

    def test_access_help_mentions_archive_and_local_override(self) -> None:
        case_def = {
            "machine": "polaris",
            "provenance": {"archive_file": "input_backup/n620fr_ST40"},
        }
        msg = self.ib._case_dir_access_help(
            "st40-heatload", case_def, "/grand/Original/owner/dir"
        )
        self.assertIn("input_backup/n620fr_ST40", msg)
        self.assertIn("st40-heatload.local.yaml", msg)
        self.assertIn("read access", msg)


if __name__ == "__main__":
    unittest.main()
