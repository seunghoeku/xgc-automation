from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import provenance as prov


class ProvenanceClassificationTests(unittest.TestCase):
    def test_registered_simulation_defaults_to_master(self) -> None:
        self.assertEqual(
            prov.classify(None, simulation_id="n645pe", hello_world=False),
            "master",
        )

    def test_unregistered_and_hello_world_runs_default_to_test(self) -> None:
        self.assertEqual(
            prov.classify(None, simulation_id=None, hello_world=False),
            "test",
        )
        self.assertEqual(
            prov.classify(None, simulation_id=None, hello_world=True),
            "test",
        )

    def test_unregistered_scientific_run_may_explicitly_use_master(self) -> None:
        self.assertEqual(
            prov.classify("master", simulation_id=None, hello_world=False),
            "master",
        )

    def test_incompatible_explicit_classes_are_rejected(self) -> None:
        with self.assertRaisesRegex(prov.ProvenanceError, "registered simulation"):
            prov.classify("test", simulation_id="n645pe", hello_world=False)
        with self.assertRaisesRegex(prov.ProvenanceError, "hello-world"):
            prov.classify("master", simulation_id=None, hello_world=True)


class ProvenanceLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name) / "runs"
        self.runs_dir.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record(self, path: Path) -> None:
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    def test_finds_classified_and_legacy_records(self) -> None:
        master = self.runs_dir / "master" / "run-master" / "provenance.json"
        legacy = self.runs_dir / "run-legacy" / "provenance.json"
        self._record(master)
        self._record(legacy)

        self.assertEqual(
            prov.find_record(self.runs_dir, "run-master"),
            (master.parent, master),
        )
        self.assertEqual(
            prov.find_record(self.runs_dir, "run-legacy"),
            (legacy.parent, legacy),
        )

    def test_duplicate_run_id_across_classes_is_ambiguous(self) -> None:
        for provenance_class in prov.PROVENANCE_CLASSES:
            self._record(
                self.runs_dir
                / provenance_class
                / "run-duplicate"
                / "provenance.json"
            )

        with self.assertRaisesRegex(prov.ProvenanceError, "ambiguous provenance"):
            prov.find_record(self.runs_dir, "run-duplicate")

    def test_iter_records_reports_storage_class(self) -> None:
        for provenance_class in prov.PROVENANCE_CLASSES:
            self._record(
                self.runs_dir / provenance_class / f"run-{provenance_class}"
                / "provenance.json"
            )
        self._record(self.runs_dir / "run-legacy" / "provenance.json")

        classes = [
            provenance_class
            for provenance_class, _ in prov.iter_records(self.runs_dir)
        ]
        self.assertEqual(classes, ["master", "test", "legacy"])


if __name__ == "__main__":
    unittest.main()
