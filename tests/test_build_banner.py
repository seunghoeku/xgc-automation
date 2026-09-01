"""Tests for XGC build-banner capture (tools/build_banner.py)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_banner as bb
from tools import hpc_runner as hr
from tools import simulation_catalog as catalog

CLEAN_BANNER = """\
MPI_COMM_WORLD NODE 3 [ nid004323 ] : ( 8 MPI TASKS ) 24-31
--------------------------------------------------------------
BUILD DATE:    Sun Aug 30 12:42:31 PM PDT 2026
HOST:          login05
BUILT BY:      sku
CXX COMPILER:  GNU 14.3.0
BUILD TYPE:    RelWithDebInfo

XGC_FLAGS:

GIT INFO:
    origin\tgit@github.com:seunghoeku/XGC-Devel.git (fetch)
    b84b1bdd0fa24d2012c650df7635c98d09da92c5 Merge remote-tracking branch 'origin/reduced_ad_response' into reduced_ad_response
    Working tree is clean.
  *** USING CPU VERSION ***
"""

DIRTY_BANNER = """\
BUILD DATE:    Fri Aug 28 15:45:00 EDT 2026
HOST:          login01
BUILT BY:      sku
CXX COMPILER:  Clang 22.1.4
BUILD TYPE:    Release

XGC_FLAGS:
    -DXGC_FLAG_ONE
    -DXGC_FLAG_TWO

GIT INFO:
    origin\tgit@github.com:seunghoeku/XGC-Devel.git (fetch)
    upstream\tgit@github.com:PrincetonUniversity/XGC-Devel.git (fetch)
    d9478c550b02661b1b5d132f494fb2bc93704c5c Merge pull request #2780
    Uncommitted changes exist.
    Whole-repo changed tracked files vs HEAD:
        M\tXGC_core/module.F90
        M\tCMakeLists.txt
    XGC_core diff preview file: git_diff_XGC_core.txt
step 1 output follows
"""

NO_GIT_BANNER = """\
BUILD DATE:    Fri Aug 28 15:45:00 EDT 2026
HOST:          login01
BUILT BY:      sku
CXX COMPILER:  GNU 14.3.0
BUILD TYPE:    Release

XGC_FLAGS:

GIT INFO:
    Build not attached to a git repository.
"""


class ParseBannerTest(unittest.TestCase):
    def test_clean_banner(self):
        banner = bb.parse_build_banner(CLEAN_BANNER)
        self.assertEqual(banner["build_date"], "Sun Aug 30 12:42:31 PM PDT 2026")
        self.assertEqual(banner["build_host"], "login05")
        self.assertEqual(banner["built_by"], "sku")
        self.assertEqual(banner["cxx_compiler"], "GNU 14.3.0")
        self.assertEqual(banner["build_type"], "RelWithDebInfo")
        self.assertEqual(banner["xgc_flags"], [])
        git = banner["git"]
        self.assertEqual(
            git["remotes"], {"origin": "git@github.com:seunghoeku/XGC-Devel.git"}
        )
        self.assertEqual(git["commit"], "b84b1bdd0fa24d2012c650df7635c98d09da92c5")
        self.assertTrue(git["commit_subject"].startswith("Merge remote-tracking"))
        self.assertEqual(git["working_tree"], "clean")

    def test_dirty_banner(self):
        banner = bb.parse_build_banner(DIRTY_BANNER)
        self.assertEqual(banner["xgc_flags"], ["-DXGC_FLAG_ONE", "-DXGC_FLAG_TWO"])
        git = banner["git"]
        self.assertEqual(len(git["remotes"]), 2)
        self.assertEqual(git["working_tree"], "dirty")
        self.assertEqual(
            git["changed_files"], ["M\tXGC_core/module.F90", "M\tCMakeLists.txt"]
        )
        self.assertEqual(git["diff_preview_file"], "git_diff_XGC_core.txt")

    def test_no_git_banner(self):
        banner = bb.parse_build_banner(NO_GIT_BANNER)
        self.assertEqual(banner["git"]["working_tree"], "no-git")
        self.assertIsNone(banner["git"]["commit"])

    def test_absent_banner(self):
        self.assertIsNone(bb.parse_build_banner("hello world job\nno banner here\n"))


class SelectStdoutFileTest(unittest.TestCase):
    def test_job_id_disambiguates_shared_directory(self):
        names = [
            "input",
            "slurm-57753077.out",
            "slurm-57784520.out",
            "slurm-57784606.out",
        ]
        self.assertEqual(
            bb.select_stdout_file(names, "57784606"), "slurm-57784606.out"
        )

    def test_pbs_output_name(self):
        self.assertEqual(
            bb.select_stdout_file(["NT.o7301673", "input"], "7301673"), "NT.o7301673"
        )

    def test_single_out_without_job_id(self):
        self.assertEqual(
            bb.select_stdout_file(["slurm-1.out", "input"], None), "slurm-1.out"
        )

    def test_ambiguous_without_job_id(self):
        self.assertIsNone(
            bb.select_stdout_file(["slurm-1.out", "slurm-2.out"], None)
        )


class FakeResult:
    def __init__(self, stdout, ok=True):
        self.stdout = stdout
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stderr = ""


class CaptureTest(unittest.TestCase):
    def _fake_ssh(self, listing, content):
        def fake_run(host, argv, **kwargs):
            if argv[0] == "ls":
                return FakeResult(listing)
            if argv[0] == "awk":
                return FakeResult(content)
            raise AssertionError(f"unexpected argv {argv}")

        return fake_run

    def test_capture_found(self):
        with (
            patch.object(bb.m, "get_machine", return_value={"auth": {}}),
            patch.object(bb.ssh, "host_for", return_value="fake"),
            patch.object(
                bb.ssh,
                "run",
                side_effect=self._fake_ssh("slurm-57784606.out\ninput", CLEAN_BANNER),
            ),
        ):
            result = bb.capture("perlmutter", "/scratch/case", "57784606")
        self.assertTrue(result["found"])
        self.assertEqual(result["source_file"], "slurm-57784606.out")
        summary = bb.catalog_summary(result)
        self.assertEqual(
            summary["commit"], "b84b1bdd0fa24d2012c650df7635c98d09da92c5"
        )
        self.assertEqual(summary["working_tree"], "clean")
        self.assertEqual(
            summary["origin_url"], "git@github.com:seunghoeku/XGC-Devel.git"
        )
        self.assertEqual(summary["build_date"], "Sun Aug 30 12:42:31 PM PDT 2026")

    def test_capture_no_banner_is_definitive(self):
        with (
            patch.object(bb.m, "get_machine", return_value={"auth": {}}),
            patch.object(bb.ssh, "host_for", return_value="fake"),
            patch.object(
                bb.ssh, "run", side_effect=self._fake_ssh("slurm-1.out", "")
            ),
        ):
            result = bb.capture("perlmutter", "/scratch/case", "1")
        self.assertFalse(result["found"])
        self.assertTrue(result["definitive"])

    def test_capture_missing_stdout_is_transient(self):
        with (
            patch.object(bb.m, "get_machine", return_value={"auth": {}}),
            patch.object(bb.ssh, "host_for", return_value="fake"),
            patch.object(bb.ssh, "run", side_effect=self._fake_ssh("input", "")),
        ):
            result = bb.capture("perlmutter", "/scratch/case", "1")
        self.assertFalse(result["found"])
        self.assertFalse(result["definitive"])


class MaybeCaptureBuildTest(unittest.TestCase):
    """hpc_runner.maybe_capture_build: catalog + provenance wiring."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog_dir = self.root / "simulations"
        self.catalog_dir.mkdir()
        self.runs_dir = self.root / "runs"
        (self.runs_dir / "master" / "run-1").mkdir(parents=True)
        (self.runs_dir / "master" / "run-1" / "provenance.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "machine": "perlmutter",
                    "job_id": "57784606",
                    "simulation_id": "n645pe",
                    "xgc": {"exe": "/x/xgc", "commit": "feedface" + "0" * 32},
                }
            )
        )
        self.record = catalog.new_record(
            record_id="n645pe",
            simulation_id="n645pe",
            number=645,
            origin="automation",
            machines=["perlmutter"],
        )
        self.record["runs"] = [
            {
                "run_id": "run-1",
                "machine": "perlmutter",
                "job_id": "57784606",
                "remote_run_dir": "/scratch/case",
                "state": "running",
            }
        ]
        catalog.write_record(self.record, catalog_dir=self.catalog_dir)

    def _patches(self, capture_result):
        return (
            patch.object(hr.catalog, "CATALOG_DIR", self.catalog_dir),
            patch.object(hr, "RUNS_DIR", self.runs_dir),
            patch.object(hr.build_banner, "capture", return_value=capture_result),
        )

    def test_capture_written_to_catalog_and_provenance(self):
        capture_result = {
            "found": True,
            "definitive": True,
            "banner": bb.parse_build_banner(CLEAN_BANNER),
            "source_file": "slurm-57784606.out",
            "captured_at": "2026-08-31T00:00:00+00:00",
        }
        p1, p2, p3 = self._patches(capture_result)
        with p1, p2, p3:
            out = hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "running")
        self.assertTrue(out["captured"])

        record = catalog.read_record("n645pe", catalog_dir=self.catalog_dir)
        build = record["runs"][0]["build"]
        self.assertEqual(build["commit"], "b84b1bdd0fa24d2012c650df7635c98d09da92c5")
        self.assertEqual(build["working_tree"], "clean")

        prov = json.loads(
            (self.runs_dir / "master" / "run-1" / "provenance.json").read_text()
        )
        self.assertEqual(
            prov["xgc"]["build"]["git"]["commit"],
            "b84b1bdd0fa24d2012c650df7635c98d09da92c5",
        )
        self.assertEqual(prov["xgc"]["build_source"]["file"], "slurm-57784606.out")
        # The plan recorded a different commit: the mismatch must be flagged.
        self.assertEqual(
            prov["xgc"]["build_commit_mismatch"]["observed"],
            "b84b1bdd0fa24d2012c650df7635c98d09da92c5",
        )

        # Second call must skip: the run already has its build.
        p1, p2, p3 = self._patches(capture_result)
        with p1, p2, p3 as mocked:
            out = hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "running")
        self.assertIsNone(out)
        mocked.assert_not_called()

    def test_no_banner_recorded_only_when_terminal(self):
        missing = {"found": False, "definitive": True, "reason": "no build banner"}
        p1, p2, p3 = self._patches(missing)
        with p1, p2, p3:
            out = hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "running")
        self.assertTrue(out["retry"])
        record = catalog.read_record("n645pe", catalog_dir=self.catalog_dir)
        self.assertNotIn("build", record["runs"][0])

        p1, p2, p3 = self._patches(missing)
        with p1, p2, p3:
            out = hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "completed")
        self.assertFalse(out["captured"])
        record = catalog.read_record("n645pe", catalog_dir=self.catalog_dir)
        self.assertEqual(record["runs"][0]["build"]["captured"], False)

    def test_pending_state_skips(self):
        p1, p2, p3 = self._patches({})
        with p1, p2, p3 as mocked:
            out = hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "pending")
        self.assertIsNone(out)
        mocked.assert_not_called()

    def test_build_commit_searchable(self):
        capture_result = {
            "found": True,
            "definitive": True,
            "banner": bb.parse_build_banner(CLEAN_BANNER),
            "source_file": "slurm-57784606.out",
            "captured_at": "2026-08-31T00:00:00+00:00",
        }
        p1, p2, p3 = self._patches(capture_result)
        with p1, p2, p3:
            hr.maybe_capture_build("perlmutter", "n645pe", "run-1", "running")
        hits = catalog.search("b84b1bdd", catalog_dir=self.catalog_dir)
        self.assertEqual([h["record_id"] for h in hits], ["n645pe"])


if __name__ == "__main__":
    unittest.main()
