from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import catalog_sync


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


class TwoCheckoutsTests(unittest.TestCase):
    """A bare origin and two clones stand in for the Mac and stellar-ai."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.a = base / "a"
        self.b = base / "b"
        for clone in (self.a, self.b):
            subprocess.run(["git", "clone", "-q", str(self.origin), str(clone)], check=True)
            _git(clone, "config", "user.email", "t@example.org")
            _git(clone, "config", "user.name", "test")
        (self.a / "kb").mkdir()
        (self.a / "kb" / "registry.yaml").write_text("next: 1\n")
        _git(self.a, "add", "-A")
        _git(self.a, "commit", "-q", "-m", "init")
        _git(self.a, "push", "-q", "-u", "origin", "main")
        _git(self.b, "pull", "-q")
        _git(self.b, "branch", "-q", "-u", "origin/main")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enabled_needs_a_repository_with_origin(self) -> None:
        self.assertTrue(catalog_sync.enabled(self.a))
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.assertFalse(catalog_sync.enabled(plain))

    def test_write_on_one_checkout_reaches_the_other(self) -> None:
        (self.a / "kb" / "n001.yaml").write_text("status: reserved\n")
        pushed = catalog_sync.commit_and_push("reserve n001", root=self.a)
        self.assertTrue(pushed["pushed"], pushed)
        pulled = catalog_sync.pull(self.b)
        self.assertTrue(pulled["pulled"], pulled)
        self.assertTrue((self.b / "kb" / "n001.yaml").exists())

    def test_nothing_to_commit_is_reported_not_failed(self) -> None:
        result = catalog_sync.commit_and_push("noop", root=self.a)
        self.assertFalse(result["committed"])
        self.assertEqual(result["detail"], "nothing to commit")

    def test_independent_records_merge_by_rebase(self) -> None:
        (self.a / "kb" / "n002.yaml").write_text("a\n")
        catalog_sync.commit_and_push("reserve n002", root=self.a)
        (self.b / "kb" / "n003.yaml").write_text("b\n")  # b has not pulled
        pushed = catalog_sync.commit_and_push("reserve n003", root=self.b)
        self.assertTrue(pushed["pushed"], pushed)
        catalog_sync.pull(self.a)
        self.assertTrue((self.a / "kb" / "n003.yaml").exists())

    def test_counter_conflict_is_rejected_and_our_commit_can_be_dropped(self) -> None:
        (self.a / "kb" / "registry.yaml").write_text("next: 2\n")
        catalog_sync.commit_and_push("reserve n001", root=self.a)
        (self.b / "kb" / "registry.yaml").write_text("next: 2 (from b)\n")
        (self.b / "kb" / "n001.yaml").write_text("b's record\n")
        pushed = catalog_sync.commit_and_push("reserve n001", root=self.b)
        self.assertTrue(pushed["rejected"], pushed)
        # not mid-rebase, commit still local
        self.assertFalse((self.b / ".git" / "rebase-merge").exists())
        dropped = catalog_sync.drop_unpushed_commit("reserve n001", root=self.b)
        self.assertTrue(dropped["dropped"], dropped)
        self.assertEqual((self.b / "kb" / "registry.yaml").read_text(), "next: 2\n")
        self.assertFalse((self.b / "kb" / "n001.yaml").exists())

    def test_drop_refuses_anything_but_our_single_commit(self) -> None:
        (self.b / "kb" / "x.yaml").write_text("x\n")
        _git(self.b, "add", "-A")
        _git(self.b, "commit", "-q", "-m", "something else")
        dropped = catalog_sync.drop_unpushed_commit("reserve n001", root=self.b)
        self.assertFalse(dropped["dropped"])
        self.assertTrue((self.b / "kb" / "x.yaml").exists())


class WroteHeuristicTests(unittest.TestCase):
    def test_plan_only_and_refused_results_do_not_commit(self) -> None:
        self.assertFalse(catalog_sync._wrote({"reserved": False, "plan": {}}))
        self.assertFalse(catalog_sync._wrote({"written": False}))
        self.assertFalse(catalog_sync._wrote({"writes": False}))
        self.assertTrue(catalog_sync._wrote({"reserved": True}))
        self.assertTrue(catalog_sync._wrote({"status": "completed"}))


if __name__ == "__main__":
    unittest.main()
