from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import machines as m
from tools import meshes

RECIPE = """!Input EFIT file containing psi grid and wall boundary.
eqdskFile tcv69271_t1.1_NT.eqd
limiterFile TCV_wall_adj.txt
reverse_psi 0
radial_uniform_meshing_unit -1
SimLic /usr/pppl/Simmetrix/simmodsuite.lic
inter_curve_spacing_file surf.txt
intra_curve_spacing_option -1
intra_curve_spacing_file dpol.txt
intra_curve_minlength_lastedge=0.05
fluxC_in_privateR_on 1
num_phi_planes 240
parallel_cutoff 0.996 !0.9999975
"""


class RecipeParsingTests(unittest.TestCase):
    def test_parse_handles_comments_equals_and_trailing_comments(self) -> None:
        params = meshes.parse_mesh_in(RECIPE)
        self.assertEqual(params["eqdskFile"], "tcv69271_t1.1_NT.eqd")
        self.assertEqual(params["intra_curve_minlength_lastedge"], "0.05")
        self.assertEqual(params["parallel_cutoff"], "0.996")
        self.assertEqual(params["num_phi_planes"], "240")
        self.assertEqual(meshes.recipe_files(params), {"surf": "surf.txt", "dpol": "dpol.txt", "limiter": "TCV_wall_adj.txt"})

    def test_render_replaces_in_place_comments_out_and_appends(self) -> None:
        text = meshes.render_mesh_in(RECIPE, {"num_phi_planes": "128", "limiterFile": None, "nodeFile": "x.node"})
        params = meshes.parse_mesh_in(text)
        self.assertEqual(params["num_phi_planes"], "128")
        self.assertNotIn("limiterFile", params)
        self.assertEqual(params["nodeFile"], "x.node")
        self.assertIn("!limiterFile TCV_wall_adj.txt", text)
        self.assertIn("!Input EFIT file", text)  # comments survive
        self.assertEqual(meshes.diff_parameters(meshes.parse_mesh_in(RECIPE), params)["num_phi_planes"], ["240", "128"])

    def test_geometry_reads_planes_and_alignment(self) -> None:
        geo = meshes.geometry(meshes.parse_mesh_in(RECIPE))
        self.assertEqual(geo, {"planes": 240, "axisymmetric": False, "field_aligned": True, "radial_mode": -1, "poloidal_option": -1})


class AttributionTests(unittest.TestCase):
    def variants(self) -> dict[str, dict[str, str]]:
        base = meshes.parse_mesh_in(RECIPE)
        return {
            "xgc_mesh_in": base,
            "xgc_mesh_in_apr_20": {**base, "num_phi_planes": "64"},
            "xgc_mesh_in_may_26": {**base, "num_phi_planes": "160"},
            "xgc_mesh_in_nphi200": {**base, "num_phi_planes": "200"},
            "xgc_mesh_in_failed": {**base, "num_phi_planes": "1"},
        }

    def test_nphi_token_picks_the_current_recipe(self) -> None:
        hit = meshes.attribute("tcv69271_nphi240_nt_jul_20_2026", self.variants())
        self.assertEqual((hit["name"], hit["attribution"]), ("xgc_mesh_in", "nphi token"))

    def test_variant_name_token_matches_the_output_name(self) -> None:
        hit = meshes.attribute("tcv69271_nt_apr_20_2026", self.variants())
        self.assertEqual((hit["name"], hit["attribution"]), ("xgc_mesh_in_apr_20", "variant name"))
        hit = meshes.attribute("tcv69271_nphi160_nt_may_26_2026", self.variants())
        self.assertEqual(hit["name"], "xgc_mesh_in_may_26")

    def test_node_file_setting_wins(self) -> None:
        variants = self.variants()
        variants["xgc_mesh_in_x"] = {**variants["xgc_mesh_in"], "nodeFile": "special.node"}
        self.assertEqual(meshes.attribute("special", variants)["attribution"], "nodeFile")

    def test_ambiguous_lists_every_candidate_and_excludes_failed(self) -> None:
        hit = meshes.attribute("tcv69271_something", self.variants())
        self.assertEqual(hit["attribution"], "ambiguous")
        self.assertEqual(hit["name"], "xgc_mesh_in")
        self.assertNotIn("xgc_mesh_in_failed", hit["candidates"])
        self.assertEqual(len(hit["candidates"]), 4)

    def test_mtime_rule_and_confidence(self) -> None:
        variants = {"xgc_mesh_in": {"num_phi_planes": "1"}, "xgc_mesh_in_d3d_xgca_robert": {"num_phi_planes": "1"}}
        mtimes = {"xgc_mesh_in": 1000.0, "xgc_mesh_in_d3d_xgca_robert": 500000.0}
        hit = meshes.attribute("au37980_coarse_Mar_25", variants, mtimes=mtimes, output_mtime=4000.0, sibling_outputs=2)
        self.assertEqual((hit["name"], hit["attribution"], hit["confidence"]), ("xgc_mesh_in", "mtime", "medium"))
        hit = meshes.attribute("au37980_Apr24", variants, mtimes=mtimes, output_mtime=9e6, sibling_outputs=2)
        self.assertEqual(hit["attribution"], "ambiguous")
        only = meshes.attribute("x", {"xgc_mesh_in": {}}, sibling_outputs=3)
        self.assertEqual((only["attribution"], only["confidence"]), ("only candidate", "low"))
        self.assertEqual(meshes.attribute("cyclone_tiny_33k", {"xgc_mesh_in": {}, "xgc_mesh_in_tiny33k": {}})["name"], "xgc_mesh_in_tiny33k")

    def test_single_candidate_and_xgca_token(self) -> None:
        self.assertEqual(meshes.attribute("whatever", {"xgc_mesh_in": {}})["attribution"], "only candidate")
        variants = {"xgc_mesh_in": {"num_phi_planes": "64"}, "xgc_mesh_in_xgca": {"num_phi_planes": "1"}}
        self.assertEqual(meshes.attribute("k030306_rmp_xgca_May_2025", variants)["name"], "xgc_mesh_in_xgca")


class DeviceInferenceTests(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(meshes.infer_device("TCV_69271_benchmark")["shot"], 69271)
        self.assertEqual(meshes.infer_device("d193793.03000_strong_NT_xgca")["device"], "d3d")
        self.assertEqual(meshes.infer_device("KSTAR_030306_7900_xgc1_coarse_rmp")["shot"], 30306)
        self.assertEqual(meshes.infer_device("nstx_139047_xgca")["device"], "nstx")
        self.assertEqual(meshes.infer_device("ITER_15MA_new_Dec_2025")["device"], "iter")
        self.assertEqual(meshes.infer_device("ASDEXU_I_mode_37980_4.200_IDE.eqdsk_XGCa")["device"], "asdexu")
        self.assertEqual(meshes.infer_device("ST40_11013565_140ms_flip")["device"], "st40")
        self.assertEqual(meshes.infer_device("cdsc2")["device"], "unknown")
        self.assertEqual(meshes.infer_device("JET_79692_again")["shot"], 79692)
        self.assertEqual(meshes.infer_device("tiny_cyclone4")["device"], "cyclone")


class FakeRemote:
    """Serves a directory tree from a dict; records what was pulled."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.pulled: list[tuple[str, Path]] = []
        self.pushed: list[tuple[Path, str]] = []
        self.scripts: list[str] = []

    def run(self, argv, *, timeout=120.0, check=True):
        if argv[0] == "find":
            root = argv[1]
            rows = []
            for path, text in self.files.items():
                if path.startswith(root + "/"):
                    rows.append(f"{path}\t{len(text)}\t{datetime(2026, 7, 20, tzinfo=timezone.utc).timestamp()}")
            return "\n".join(rows) + "\n"
        raise AssertionError(argv)

    def shell(self, script, *, timeout=120.0):
        import hashlib
        import re
        import shlex

        self.scripts.append(script)
        if script.startswith("sha256sum"):
            out = []
            for path in shlex.split(script)[2:]:
                if path in self.files:
                    out.append(f"{hashlib.sha256(self.files[path].encode()).hexdigest()}  {path}")
            return "\n".join(out) + "\n"
        if "head -n 1" in script:
            out = []
            for path in re.findall(r"printf '%s ' (\S+);", script):
                path = shlex.split(path)[0]
                out.append(f"{path} {self.files[path].splitlines()[0].split()[0]}")
            return "\n".join(out) + "\n"
        if meshes._FILE_MARK in script:
            out = []
            for path in re.findall(r"cat (\S+) 2>/dev/null", script):
                path = shlex.split(path)[0]
                out.append(f"{meshes._FILE_MARK} {path}\n{self.files.get(path, '')}")
            return "".join(out)
        if script.startswith("test -e"):
            return "no\n"
        return ""

    def pull(self, remote_path, local_path, *, timeout=600.0):
        self.pulled.append((remote_path, local_path))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(self.files[remote_path])

    def push(self, local_path, remote_path, *, timeout=600.0):
        self.pushed.append((Path(local_path), remote_path))

    def write_text(self, path, content):
        self.files[path] = content


ROOT = "/p/mesh"
NODE = "3 2 0 1\n1 0.9 -0.04 0\n2 0.94 -0.03 0\n3 0.92 0.27 0\n"
ELE = "1 3 0\n1 1 2 3\n"


def tree() -> dict[str, str]:
    d = f"{ROOT}/TCV_69271_benchmark"
    return {
        f"{d}/xgc_mesh_in": RECIPE,
        f"{d}/xgc_mesh_in_apr_20": RECIPE.replace("num_phi_planes 240", "num_phi_planes 64"),
        f"{d}/xgc_mesh_in_failed": RECIPE.replace("num_phi_planes 240", "num_phi_planes 1"),
        f"{d}/surf.txt": "2\n0.0\n1.0\n",
        f"{d}/dpol.txt": "2\n0.0 0.003\n1.0 0.003\n",
        f"{d}/TCV_wall_adj.txt": "2\n0.6 0.0\n0.6 0.1\n",
        f"{d}/tcv69271_t1.1_NT.eqd": "equilibrium bytes",
        f"{d}/tcv69271_nphi240_nt_jul_20_2026.node": NODE,
        f"{d}/tcv69271_nphi240_nt_jul_20_2026.ele": ELE,
        f"{d}/tcv69271_nphi240_nt_jul_20_2026.flx.aif": "1\n",
        f"{d}/tcv69271_nt_apr_20_2026.node": NODE.replace("0.27", "0.28"),
        f"{d}/tcv69271_nt_apr_20_2026.ele": ELE,
        f"{d}/tcv69271_nt_apr_20_2026.flx.aif": "1\n",
        f"{ROOT}/KSTAR_crashed/xgc_mesh_in": RECIPE.replace("num_phi_planes 240", "num_phi_planes 1"),
        f"{ROOT}/KSTAR_crashed/surf.txt": "1\n0.5\n",
    }


MACHINES = {
    "flux": {
        "center": "pppl",
        "auth": {"method": "ssh_controlmaster", "ssh_config_host": "flux"},
        "mesh": {"root": ROOT, "tomms": {"exe": "/opt/TOMMS/2025_11_19/bin/tomms_meshgen",
                                         "env": ["module load intel", "export OMP_NUM_THREADS=1"],
                                         "license": "/lic/sim.lic"}},
    },
    "perlmutter": {"center": "nersc", "auth": {"method": "sshproxy"}},
}


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.meshes_dir = root / "kb" / "meshes"
        self.data_root = root / "data"
        self.remote = FakeRemote(tree())
        self.ctx = m.using_machines(MACHINES)
        self.ctx.__enter__()

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    def run_import(self, **kwargs):
        return meshes.import_from_remote(
            "flux", since=None, meshes_dir=self.meshes_dir, data_root=self.data_root,
            catalog_dir=Path(self.tmp.name) / "simulations", remote=self.remote, **kwargs
        )

    def test_dry_run_writes_nothing_and_reports_attribution(self) -> None:
        result = self.run_import(dry_run=True)
        self.assertFalse(self.meshes_dir.exists())
        by_id = {r["mesh_id"]: r for r in result["records"]}
        self.assertEqual(by_id["tcv69271_nphi240_nt_jul_20_2026"]["attribution"], "nphi token")
        self.assertEqual(by_id["tcv69271_nt_apr_20_2026"]["recipe"], "xgc_mesh_in_apr_20")
        self.assertEqual(by_id["KSTAR_crashed"]["attribution"], "no outputs")
        self.assertEqual(result["ambiguous"], [])

    def test_import_writes_records_recipes_and_equilibrium(self) -> None:
        result = self.run_import()
        self.assertEqual(sorted(result["written"]),
                         ["KSTAR_crashed__no_output", "tcv69271_nphi240_nt_jul_20_2026", "tcv69271_nt_apr_20_2026"])
        record = meshes.load("tcv69271_nphi240_nt_jul_20_2026", meshes_dir=self.meshes_dir)
        self.assertEqual(record["status"], "ok")
        self.assertEqual((record["device"], record["shot"]), ("tcv", 69271))
        self.assertEqual(record["outputs"]["nodes"], 3)
        self.assertEqual(record["outputs"]["triangles"], 1)
        self.assertEqual(record["geometry"]["planes"], 240)
        self.assertEqual(record["recipe"]["attribution"], "nphi token")
        self.assertTrue((self.meshes_dir / "tcv69271_nphi240_nt_jul_20_2026" / "xgc_mesh_in").is_file())
        self.assertTrue((self.meshes_dir / "tcv69271_nphi240_nt_jul_20_2026" / "dpol.txt").is_file())
        self.assertEqual(record["inputs"]["limiter"]["name"], "TCV_wall_adj.txt")
        eq = record["inputs"]["equilibrium"]
        self.assertTrue((self.data_root / eq["local"]).is_file())
        self.assertEqual(record["attempts"][0]["name"], "xgc_mesh_in_failed")
        self.assertEqual(record["attempts"][0]["parameters_diff"], {"num_phi_planes": ["240", "1"]})
        failed = meshes.load("KSTAR_crashed__no_output", meshes_dir=self.meshes_dir)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["device"], "kstar")
        # A dpol.txt the recipe names but the directory lacks is reported, not invented.
        self.assertIn("KSTAR_crashed__no_output: dpol dpol.txt", result["missing_inputs"])
        self.assertIn("KSTAR_crashed__no_output: limiter TCV_wall_adj.txt", result["missing_inputs"])

    def test_reimport_keeps_user_fields_and_pulls_equilibrium_once(self) -> None:
        self.run_import()
        meshes.annotate("tcv69271_nt_apr_20_2026", note="first TCV NT mesh", device="tcv", shot=69271, meshes_dir=self.meshes_dir)
        pulled = len(self.remote.pulled)
        self.run_import()
        record = meshes.load("tcv69271_nt_apr_20_2026", meshes_dir=self.meshes_dir)
        self.assertEqual(record["note"], "first TCV NT mesh")
        self.assertEqual(record["device_basis"], "set by the user")
        self.assertEqual(len(self.remote.pulled), pulled)

    def test_same_mesh_in_two_directories_is_one_record(self) -> None:
        copy_dir = f"{ROOT}/TCV_69271_copy"
        for name in ("tcv69271_nt_apr_20_2026.node", "tcv69271_nt_apr_20_2026.ele", "tcv69271_nt_apr_20_2026.flx.aif", "xgc_mesh_in"):
            self.remote.files[f"{copy_dir}/{name}"] = self.remote.files[f"{ROOT}/TCV_69271_benchmark/{name}"]
        result = self.run_import()
        self.assertEqual(result["duplicates"], [{"mesh_id": "tcv69271_nt_apr_20_2026", "also_in": f"{copy_dir}/tcv69271_nt_apr_20_2026"}])
        record = meshes.load("tcv69271_nt_apr_20_2026", meshes_dir=self.meshes_dir)
        self.assertEqual(record["source"]["also_in"], [f"{copy_dir}/tcv69271_nt_apr_20_2026"])
        self.assertFalse(meshes.record_path("tcv69271_nt_apr_20_2026__TCV_69271_copy", meshes_dir=self.meshes_dir).exists())

    def test_since_filter_skips_old_directories(self) -> None:
        result = meshes.import_from_remote(
            "flux", since="2026-08-01", dry_run=True, meshes_dir=self.meshes_dir, data_root=self.data_root, remote=self.remote,
        )
        self.assertEqual(result["directories_selected"], [])
        self.assertIsNone(meshes.parse_since(None))
        self.assertLess(meshes.parse_since("2y"), datetime.now(timezone.utc).timestamp())

    def test_link_fills_used_by_from_output_hashes(self) -> None:
        self.run_import()
        record = meshes.load("tcv69271_nphi240_nt_jul_20_2026", meshes_dir=self.meshes_dir)
        digest = record["outputs"]["node"]["sha256"]
        records = [
            {"record_id": "n694pe", "referenced_inputs": {"files": {"a.node": {"sha256": digest}}}},
            {"record_id": "n001", "runs": [{"referenced_inputs": {"files": {"b": {"sha256": "0" * 64}}}}]},
        ]
        result = meshes.link(meshes_dir=self.meshes_dir, records=records)
        self.assertEqual(result["linked"], {"tcv69271_nphi240_nt_jul_20_2026": ["n694pe"]})
        self.assertEqual(meshes.lookup(digest, meshes_dir=self.meshes_dir)[0]["role"], "node")
        self.assertEqual(meshes.search("nphi240", meshes_dir=self.meshes_dir)[0]["used_by"], ["n694pe"])


class GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.meshes_dir = root / "kb" / "meshes"
        self.data_root = root / "data"
        self.remote = FakeRemote(tree())
        self.ctx = m.using_machines(MACHINES)
        self.ctx.__enter__()
        self.catalog_dir = root / "simulations"
        meshes.import_from_remote("flux", since=None, meshes_dir=self.meshes_dir, data_root=self.data_root,
                                  catalog_dir=self.catalog_dir, remote=self.remote)

    def tearDown(self) -> None:
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_plan_forces_output_names_and_lists_inputs(self) -> None:
        plan = meshes.plan_generation("tcv69271_nphi128_test", base="tcv69271_nphi240_nt_jul_20_2026",
                                      overrides={"num_phi_planes": "128"}, machine="flux", meshes_dir=self.meshes_dir)
        self.assertFalse(plan["writes"])
        self.assertEqual(plan["work_dir"], f"{ROOT}/tcv69271_nphi128_test")
        params = meshes.parse_mesh_in(plan["xgc_mesh_in"])
        self.assertEqual(params["nodeFile"], "tcv69271_nphi128_test.node")
        self.assertEqual(params["num_phi_planes"], "128")
        self.assertEqual(params["SimLic"], "/lic/sim.lic")
        self.assertEqual(plan["parameters_diff"]["num_phi_planes"], ["240", "128"])
        self.assertEqual(plan["tomms"]["version"], "2025_11_19")
        roles = {s["role"]: s["from"] for s in plan["stage"]}
        self.assertEqual(roles, {"surf": "catalog copy", "dpol": "catalog copy", "limiter": "catalog copy", "equilibrium": "remote copy"})
        self.assertEqual(plan["missing"], [])
        self.assertIn("module load intel", plan["command"])

    def test_input_absent_in_base_does_not_block(self) -> None:
        meshes.annotate("tcv69271_nphi240_nt_jul_20_2026", meshes_dir=self.meshes_dir)
        record = meshes.load("tcv69271_nphi240_nt_jul_20_2026", meshes_dir=self.meshes_dir)
        record["inputs"]["private_surf"] = {"name": "priv.dat", "missing": True}
        record["parameters"]["inter_curve_spacing_private_file"] = "priv.dat"
        meshes._write(record, meshes_dir=self.meshes_dir)
        (self.meshes_dir / "tcv69271_nphi240_nt_jul_20_2026" / "xgc_mesh_in").write_text(
            RECIPE + "inter_curve_spacing_private_file priv.dat\n")
        plan = meshes.plan_generation("t2", base="tcv69271_nphi240_nt_jul_20_2026", machine="flux", meshes_dir=self.meshes_dir)
        self.assertEqual(plan["missing"], [])
        self.assertEqual({s["role"]: s["from"] for s in plan["stage"]}["private_surf"], "absent in base (not staged)")

    def test_start_requires_confirm_then_stages_and_records(self) -> None:
        plan = meshes.plan_generation("tcv69271_nphi128_test", base="tcv69271_nphi240_nt_jul_20_2026",
                                      overrides={"num_phi_planes": "128"}, machine="flux", meshes_dir=self.meshes_dir)
        refused = meshes.start_generation(plan, meshes_dir=self.meshes_dir, remote=self.remote)
        self.assertFalse(refused["started"])
        started = meshes.start_generation(plan, confirm=True, meshes_dir=self.meshes_dir, remote=self.remote)
        self.assertTrue(started["started"])
        self.assertIn(f"{ROOT}/tcv69271_nphi128_test/xgc_mesh_in", self.remote.files)
        self.assertEqual(len(self.remote.pushed), 3)
        self.assertTrue(any("nohup setsid bash -lc" in s for s in self.remote.scripts))
        record = meshes.load("tcv69271_nphi128_test", meshes_dir=self.meshes_dir)
        self.assertEqual((record["status"], record["origin"]), ("running", "automation"))
        self.assertEqual(record["device"], "tcv")
        self.assertEqual(record["source"]["base"], "tcv69271_nphi240_nt_jul_20_2026")
        with self.assertRaisesRegex(meshes.MeshIndexError, "already exists"):
            meshes.plan_generation("tcv69271_nphi128_test", base="tcv69271_nphi240_nt_jul_20_2026", machine="flux", meshes_dir=self.meshes_dir)

    def test_check_closes_the_record_when_outputs_appear(self) -> None:
        plan = meshes.plan_generation("tcv69271_nphi128_test", base="tcv69271_nphi240_nt_jul_20_2026",
                                      machine="flux", meshes_dir=self.meshes_dir)
        meshes.start_generation(plan, confirm=True, meshes_dir=self.meshes_dir, remote=self.remote)
        work = f"{ROOT}/tcv69271_nphi128_test"
        self.remote.files[f"{work}/tomms.rc"] = "0\n"
        self.remote.files[f"{work}/tomms.out"] = "done\n"
        for suffix, text in ((".node", NODE), (".ele", ELE), (".flx.aif", "1\n")):
            self.remote.files[f"{work}/tcv69271_nphi128_test{suffix}"] = text
        original_shell = self.remote.shell

        def shell(script, *, timeout=120.0):
            if script.startswith("cat ") and "tomms.rc" in script:
                return "0\n"
            if script.startswith("tail "):
                return "done\n"
            return original_shell(script, timeout=timeout)

        self.remote.shell = shell  # type: ignore[assignment]
        result = meshes.check_generation("tcv69271_nphi128_test", meshes_dir=self.meshes_dir,
                                         catalog_dir=self.catalog_dir, remote=self.remote)
        self.assertEqual(result["status"], "ok")
        record = meshes.load("tcv69271_nphi128_test", meshes_dir=self.meshes_dir)
        self.assertEqual(record["outputs"]["nodes"], 3)
        self.assertEqual(record["tomms"]["exit_code"], 0)

    def test_deploy_plans_then_copies_and_verifies(self) -> None:
        plan = meshes.deploy("tcv69271_nphi240_nt_jul_20_2026", "perlmutter", "/scratch/mesh", meshes_dir=self.meshes_dir)
        self.assertFalse(plan["deployed"])
        self.assertEqual(plan["plan"]["via"], "pull to data directory, then push")
        target = FakeRemote({})
        import hashlib

        def shell(script, *, timeout=120.0):
            if script.startswith("sha256sum"):
                return "\n".join(
                    f"{hashlib.sha256(tree()[f'{ROOT}/TCV_69271_benchmark/' + p.rsplit('/', 1)[-1]].encode()).hexdigest()}  {p}"
                    for p in script.split("-- ", 1)[1].split() if not p.startswith("2>")
                )
            return ""

        target.shell = shell  # type: ignore[assignment]
        result = meshes.deploy("tcv69271_nphi240_nt_jul_20_2026", "perlmutter", "/scratch/mesh", confirm=True,
                               meshes_dir=self.meshes_dir, data_root=self.data_root, source_remote=self.remote, target_remote=target)
        self.assertTrue(result["deployed"], result)
        self.assertEqual(len(target.pushed), 3)
        record = meshes.load("tcv69271_nphi240_nt_jul_20_2026", meshes_dir=self.meshes_dir)
        self.assertEqual(record["deployments"][0]["machine"], "perlmutter")
        self.assertTrue(record["deployments"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
