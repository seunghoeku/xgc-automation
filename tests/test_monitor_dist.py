from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import monitor_dist


class MonitorDistributionTests(unittest.TestCase):
    def test_export_contains_only_package_and_explicit_core_not_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "source"
            monitor_dist.export(target)
            paths = {p.relative_to(target).as_posix()
                     for p in target.rglob("*") if p.is_file()}
            core = target / "src/hpc_monitor/core"
            self.assertEqual({p.name for p in core.iterdir()},
                             set(monitor_dist.CORE) | {"__init__.py"})
            for path in paths:
                self.assertTrue(
                    path in {"README.md", "pyproject.toml", "SOURCE.json"}
                    or path.startswith(("src/hpc_monitor/", "tests/")), path)
                self.assertNotIn("machines.local", path)
                self.assertNotIn("servers.yaml", path)
            manifest = json.loads((target / "SOURCE.json").read_text())
            for name in monitor_dist.CORE:
                self.assertEqual(manifest["sha256"][f"tools/{name}"],
                                 hashlib.sha256((core / name).read_bytes()).hexdigest())
            # Exporting must not erase a previous build or anything placed in it.
            with self.assertRaises(FileExistsError):
                monitor_dist.export(target)


if __name__ == "__main__":
    unittest.main()
