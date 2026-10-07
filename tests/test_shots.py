from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import shots


class ShotIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.shots_dir = root / "kb" / "shots"
        self.data_root = root / "data"
        self.src = root / "inbox" / "ti_tcv69271_t1.1_NT.prf"
        self.src.parent.mkdir()
        self.src.write_text("0.0 1200\n0.5 800\n1.0 100\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_copies_into_the_data_directory_and_indexes_it(self) -> None:
        entry = shots.register_file(
            "TCV", 69271, self.src, kind="profile", diagnostic="CXRS system 3",
            quantity="T_i", columns={"psi_n": "normalised poloidal flux", "ti": "ion temperature [eV]"},
            origin="e-mail from the TCV team, 2026-05", shots_dir=self.shots_dir, data_root=self.data_root,
        )
        self.assertEqual(entry["path"], "shots/tcv/69271/ti_tcv69271_t1.1_NT.prf")
        self.assertTrue((self.data_root / entry["path"]).is_file())
        self.assertEqual(entry["sha256"], shots.sha256_of(self.src))
        data = shots.load("tcv", 69271, shots_dir=self.shots_dir)
        self.assertEqual(data["files"][0]["columns"]["ti"], "ion temperature [eV]")
        self.assertEqual((self.shots_dir / "tcv_69271.yaml").name, "tcv_69271.yaml")

    def test_reregistering_same_content_keeps_known_fields(self) -> None:
        shots.register_file("tcv", 69271, self.src, kind="profile", diagnostic="CXRS system 3",
                            shots_dir=self.shots_dir, data_root=self.data_root)
        entry = shots.register_file("tcv", 69271, self.src, kind="profile",
                                    shots_dir=self.shots_dir, data_root=self.data_root)
        self.assertEqual(entry["diagnostic"], "CXRS system 3")
        self.assertEqual(len(shots.load("tcv", 69271, shots_dir=self.shots_dir)["files"]), 1)

    def test_conflicting_content_under_the_same_name_is_refused(self) -> None:
        shots.register_file("tcv", 69271, self.src, shots_dir=self.shots_dir, data_root=self.data_root)
        other = self.src.parent / "other" / self.src.name
        other.parent.mkdir()
        other.write_text("different\n")
        with self.assertRaisesRegex(shots.ShotIndexError, "different content"):
            shots.register_file("tcv", 69271, other, shots_dir=self.shots_dir, data_root=self.data_root)

    def test_link_fills_used_by_from_catalog_hashes(self) -> None:
        entry = shots.register_file("tcv", 69271, self.src, shots_dir=self.shots_dir, data_root=self.data_root)
        records = [
            {"record_id": "n692pe", "referenced_inputs": {"files": {"ti.prf": {"sha256": entry["sha256"]}}}},
            {"record_id": "n675pe", "runs": [{"referenced_inputs": {"files": {"x": {"sha256": entry["sha256"]}}}}]},
            {"record_id": "n001", "referenced_inputs": {"files": {"y": {"sha256": "0" * 64}}}},
        ]
        result = shots.link(shots_dir=self.shots_dir, records=records)
        self.assertEqual(result["shots_updated"], 1)
        data = shots.load("tcv", 69271, shots_dir=self.shots_dir)
        self.assertEqual(data["files"][0]["used_by"], ["n675pe", "n692pe"])
        self.assertEqual(shots.summarize(data)["used_by"], ["n675pe", "n692pe"])
        self.assertEqual(shots.lookup(entry["sha256"], shots_dir=self.shots_dir)[0]["shot"], 69271)

    def test_describe_sets_shot_level_fields(self) -> None:
        data = shots.describe("tcv", 69271, description="NT discharge", time_ms=1100, shots_dir=self.shots_dir)
        self.assertEqual((data["description"], data["time_ms"]), ("NT discharge", 1100))
        self.assertEqual([s["shot"] for s in shots.iter_shots(shots_dir=self.shots_dir)], [69271])


if __name__ == "__main__":
    unittest.main()
