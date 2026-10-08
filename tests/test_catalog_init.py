from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tools import catalog_init
from tools import simulation_registry as sr


class InitCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "xgc-catalog"
        self.config = self.root / "cfg" / "config.yaml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init(self, **kw):
        return catalog_init.init_catalog(
            "alice-b", repo=self.repo, data_dir=self.root / "data", config_path=self.config, **kw
        )

    def test_creates_the_canonical_layout_and_a_fresh_registry(self) -> None:
        result = self._init()
        user = self.repo / "users" / "alice-b"
        for rel in catalog_init.LAYOUT_DIRS:
            self.assertTrue((user / rel).is_dir(), rel)
        self.assertTrue((self.repo / ".gitignore").is_file())
        self.assertTrue((self.repo / "README.md").is_file())
        self.assertTrue((user / "USER.md").is_file())
        registry = user / "kb" / "simulation_registry.yaml"
        data = yaml.safe_load(registry.read_text())
        self.assertEqual((data["next_number"], data["next_dev_number"]), (1, 1))
        self.assertEqual(data["machine_codes"]["perlmutter"], "pe")
        with patch.object(sr, "REGISTRY_PATH", registry):
            plan = sr.plan_simulation_id("perlmutter", "first", kind="production")
        self.assertEqual(plan["simulation_id"], "n001pe")
        self.assertEqual(Path(result["user_dir"]).resolve(), user.resolve())

    def test_writes_config_and_user_md_link(self) -> None:
        self._init()
        settings = yaml.safe_load(self.config.read_text())
        self.assertEqual(Path(settings["catalog_dir"]).resolve(), (self.repo / "users" / "alice-b").resolve())
        self.assertEqual(Path(settings["data_dir"]).resolve(), (self.root / "data").resolve())
        link = self.config.parent / "USER.md"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.repo / "users" / "alice-b" / "USER.md").resolve())

    def test_is_idempotent_and_keeps_existing_files(self) -> None:
        self._init()
        (self.repo / "users" / "alice-b" / "USER.md").write_text("filled in\n")
        again = self._init()
        self.assertEqual((self.repo / "users" / "alice-b" / "USER.md").read_text(), "filled in\n")
        self.assertIn("users/alice-b/USER.md", again["kept"])
        self.assertIn(str(self.config), again["kept"])

    def test_git_repository_is_initialised_and_remote_recorded(self) -> None:
        self._init(remote="git@example.org:alice/xgc-catalog.git")
        self.assertTrue((self.repo / ".git").is_dir())
        url = subprocess.run(["git", "-C", str(self.repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(url, "git@example.org:alice/xgc-catalog.git")

    def test_invalid_github_id_is_rejected(self) -> None:
        with self.assertRaises(catalog_init.CatalogInitError):
            catalog_init.init_catalog("bad id!", repo=self.repo, write_config=False, use_git=False)

    def test_layout_check(self) -> None:
        self.assertTrue(catalog_init.layout_check(self.repo / "users" / "alice-b")["canonical"])
        self.assertFalse(catalog_init.layout_check(self.repo)["canonical"])


if __name__ == "__main__":
    unittest.main()
