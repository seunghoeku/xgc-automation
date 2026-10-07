from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import paths


class StateRootResolutionTests(unittest.TestCase):
    """XGC_CATALOG_DIR > config file > the repository itself."""

    def test_default_is_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, source = paths.resolve_state_root({}, config=Path(tmp) / "missing.yaml")
        self.assertEqual(root, paths.PROJECT_ROOT)
        self.assertEqual(source, "repository (default)")

    def test_environment_variable_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("catalog_dir: /elsewhere\n", encoding="utf-8")
            root, source = paths.resolve_state_root(
                {"XGC_CATALOG_DIR": tmp}, config=config
            )
        self.assertEqual(root, Path(tmp).resolve())
        self.assertEqual(source, "$XGC_CATALOG_DIR")

    def test_config_file_relative_path_is_taken_from_its_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "cfg" / "config.yaml"
            config.parent.mkdir()
            config.write_text("catalog_dir: ../catalog\n", encoding="utf-8")
            root, source = paths.resolve_state_root({}, config=config)
        self.assertEqual(root, (Path(tmp) / "catalog").resolve())
        self.assertEqual(source, str(config))

    def test_config_file_without_the_key_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("something_else: 1\n", encoding="utf-8")
            root, _ = paths.resolve_state_root({}, config=config)
        self.assertEqual(root, paths.PROJECT_ROOT)

    def test_config_file_location_is_overridable(self) -> None:
        self.assertEqual(
            paths.config_file({"XGC_AUTOMATION_CONFIG": "/tmp/x.yaml"}), Path("/tmp/x.yaml")
        )
        self.assertEqual(
            paths.config_file({}), Path("~/.config/xgc-automation/config.yaml").expanduser()
        )


class LayoutTests(unittest.TestCase):
    def test_shared_paths_stay_in_the_repository_and_state_follows_the_root(self) -> None:
        rows = {row["name"]: row for row in paths.layout()}
        for name in ("machines_file", "templates", "cases"):
            self.assertEqual(rows[name]["kind"], "shared")
            self.assertTrue(Path(rows[name]["path"]).is_relative_to(paths.PROJECT_ROOT))
        for name in ("catalog", "registry", "runs", "input_backup", "cache", "f_init_cases"):
            self.assertEqual(rows[name]["kind"], "state")
            self.assertTrue(Path(rows[name]["path"]).is_relative_to(paths.STATE_ROOT))

    def test_modules_take_their_constants_from_paths(self) -> None:
        from tools import hpc_runner, machines, notebook_csv, simulation_catalog, simulation_registry

        self.assertEqual(hpc_runner.RUNS_DIR, paths.RUNS_DIR)
        self.assertEqual(hpc_runner.INPUT_BACKUP_DIR, paths.INPUT_BACKUP_DIR)
        self.assertEqual(simulation_catalog.CATALOG_DIR, paths.CATALOG_DIR)
        self.assertEqual(simulation_registry.REGISTRY_PATH, paths.REGISTRY_PATH)
        self.assertEqual(notebook_csv.NOTEBOOK_PATH, paths.NOTEBOOK_PATH)
        self.assertEqual(machines.LOCAL_MACHINES_FILE, paths.LOCAL_MACHINES_FILE)


class DataRootTests(unittest.TestCase):
    """Products (PNG, NPZ, logs) go to the data directory, mirrored; text stays."""

    def test_data_root_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("data_dir: ./products\n", encoding="utf-8")
            root, source = paths.resolve_data_root({"XGC_DATA_DIR": tmp}, config=config)
            self.assertEqual((root, source), (Path(tmp).resolve(), "$XGC_DATA_DIR"))
            root, source = paths.resolve_data_root({}, config=config)
            self.assertEqual(root, (Path(tmp) / "products").resolve())
            self.assertEqual(source, str(config))
            root, source = paths.resolve_data_root(
                {}, config=Path(tmp) / "missing.yaml", state_root=Path(tmp)
            )
            self.assertEqual(root, Path(tmp) / "data")

    def test_product_target_mirrors_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state, data = Path(tmp) / "catalog", Path(tmp) / "data"
            state.mkdir()
            png = state / "runs" / "master" / "run-x" / "summaries" / "a.png"
            self.assertEqual(
                paths.product_target(png, state_root=state, data_root=data),
                data / "runs" / "master" / "run-x" / "summaries" / "a.png",
            )
            json_path = png.with_suffix(".json")
            self.assertEqual(
                paths.product_target(json_path, state_root=state, data_root=data), json_path
            )
            elsewhere = Path(tmp) / "other" / "b.npz"
            self.assertEqual(
                paths.product_target(elsewhere, state_root=state, data_root=data), elsewhere
            )

    def test_product_suffixes(self) -> None:
        for name in ("a.png", "b.NPZ", "job.out", "c.svg"):
            self.assertTrue(paths.is_product(name), name)
        for name in ("provenance.json", "input", "submit.sh", "plot.py"):
            self.assertFalse(paths.is_product(name), name)


if __name__ == "__main__":
    unittest.main()
