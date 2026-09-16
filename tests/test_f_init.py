from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from f_init import config as cfg
from f_init import namelist_edit as nle
from f_init import procedure as proc
from f_init import sbatch as sb


INPUT = """&sml_param
sml_dt=2.0D-3             ! delta-t for one time step - unit of toroidal transit time.
sml_mstep=2000           ! total time step
sml_restart=.true.
/
&ptl_param
ptl_nsp=1
ptl_input_file_dir='/pscratch/sd/s/sku/n570pe_NT_xgc1_d3d_turb/'
ptl_f_init_file='xgc.f_init_n570_e.bp' 'xgc.f_init_n570_D.bp' 'xgc.f_init_n570_C.bp'
ptl_maxwellian_init=.true. .true. .true. 
/
"""

XGCS = """#!/bin/bash
#SBATCH --account=m499
#SBATCH --qos=debug
##SBATCH --qos=regular
#SBATCH --constraint=cpu
#SBATCH --job-name=maxwell
#SBATCH --nodes=8
#SBATCH --time=0:30:00
#SBATCH --dependency=afterany:27864772
export OMP_PLACES=cores
./runscript.cpu
"""

RUNSCRIPT = """module load cray-fftw
module unload darshan
export XGC_BIN=../../../XGC-Devel/bin_cpu/bin/xgca
export OMP_NUM_THREADS=32
export n_mpi_ranks_per_node=8
srun -n 64 $XGC_BIN
"""


class NamelistEditTests(unittest.TestCase):
    def test_array_slot_replaced_and_quotes_kept(self) -> None:
        raw = nle.read_value(INPUT, "ptl_f_init_file")
        new = nle.set_slot(raw, 2, "'xgc.f_init_no_smooth.bp'")
        self.assertEqual(
            new, "'xgc.f_init_n570_e.bp' 'xgc.f_init_no_smooth.bp' 'xgc.f_init_n570_C.bp'"
        )
        self.assertEqual(nle.set_slot(".true. .true. .true.", 2, ".false."),
                         ".true. .false. .true.")

    def test_slot_out_of_range_is_an_error(self) -> None:
        with self.assertRaises(nle.NamelistEditError):
            nle.set_slot(".true. .true.", 3, ".false.")

    def test_apply_edits_in_place_and_inserts_absent(self) -> None:
        text, notes = nle.apply(INPUT, {"sml_dt": "2.0D-13", "sml_bounce": "2"})
        self.assertIn("sml_dt=2.0D-13  ! delta-t", text)
        self.assertIn("&sml_param\nsml_bounce=2\n", text)
        self.assertEqual(len(notes), 2)

    def test_array_slot_reads_unquoted(self) -> None:
        self.assertEqual(nle.array_slot(INPUT, "ptl_f_init_file", 2), "xgc.f_init_n570_D.bp")
        self.assertIsNone(nle.array_slot(INPUT, "ptl_f_init_file", 4))


class SbatchTests(unittest.TestCase):
    def test_directives_ignore_commented_lines(self) -> None:
        directives = sb.parse_directives(XGCS)
        self.assertEqual(directives["qos"], "debug")
        self.assertEqual(directives["nodes"], "8")

    def test_rewrite_sets_name_and_drops_dependency(self) -> None:
        text, notes = sb.rewrite(XGCS, "cbc6_dt0_no_smooth")
        self.assertIn("#SBATCH --job-name=cbc6_dt0_no_smooth\n", text)
        self.assertNotIn("--dependency", text)
        self.assertTrue(any("dropped" in n for n in notes))

    def test_resources_estimate_node_hours(self) -> None:
        res = sb.resources(XGCS)
        self.assertEqual(res["nodes"], 8)
        self.assertEqual(res["node_hours"], 4.0)
        self.assertEqual(sb.walltime_hours("1-12:00:00"), 36.0)
        self.assertEqual(sb.walltime_hours("45"), 0.75)

    def test_runscript_parse(self) -> None:
        info = sb.parse_runscript(RUNSCRIPT)
        self.assertEqual(info["xgc_bin"], "../../../XGC-Devel/bin_cpu/bin/xgca")
        self.assertEqual(info["modules"], ["module load cray-fftw", "module unload darshan"])
        self.assertEqual(info["ranks_per_node"], 8)


class NamingTests(unittest.TestCase):
    def test_manual_number_format(self) -> None:
        self.assertEqual(cfg.smooth_tag(0.3, 0.5), "0.3_0.5")
        self.assertEqual(cfg.smooth_tag(0, 0), "0_0")
        self.assertEqual(cfg.smooth_tag(1.0, 0.5), "1_0.5")

    def test_dt0_and_adjusted_names(self) -> None:
        self.assertEqual(cfg.dt0_dirname("xgc.f_init_smooth_0.3_0.5.bp"), "dt0_smooth_0.3_0.5")
        self.assertEqual(cfg.adjusted_file("xgc.f_init_no_smooth.bp"),
                         "xgc.f_init_adjusted_no_smooth.bp")
        self.assertEqual(cfg.adjusted_file("xgc.f_init_adjusted_no_smooth.bp"),
                         "xgc.f_init_adjusted2_no_smooth.bp")
        self.assertEqual(cfg.adjusted_file("xgc.f_init_adjusted2_smooth_1_0.5.bp"),
                         "xgc.f_init_adjusted3_smooth_1_0.5.bp")

    def test_example_case_refuses_placeholder_root(self) -> None:
        with self.assertRaises(cfg.CaseError):
            cfg.load_case("example")

    def test_cbc6_case_loads_with_defaults(self) -> None:
        case = cfg.load_case("cbc6")
        self.assertEqual(case.maxwell_path, "/pscratch/sd/s/sku/f_init/cbc6/maxwell")
        self.assertEqual(case.species["slot"], 2)
        self.assertEqual(case.job["script"], "xgcs.cpu")

    def test_single_species_names_are_untagged(self) -> None:
        self.assertEqual(cfg.no_smooth_file(), cfg.NO_SMOOTH_FILE)
        self.assertEqual(cfg.smooth_file(0.5, 0.5), "xgc.f_init_smooth_0.5_0.5.bp")

    def test_species_tag_survives_the_adjusted_prefix(self) -> None:
        # The tag goes in front, so adjusted/adjusted2 still parse and the
        # species stays visible in every derived name.
        tagged = cfg.smooth_file(0.5, 0.5, "D")
        self.assertEqual(tagged, "xgc.f_init_D_smooth_0.5_0.5.bp")
        self.assertEqual(cfg.adjusted_file(tagged), "xgc.f_init_adjusted_D_smooth_0.5_0.5.bp")
        self.assertEqual(cfg.adjusted_file(cfg.adjusted_file(tagged)),
                         "xgc.f_init_adjusted2_D_smooth_0.5_0.5.bp")
        self.assertEqual(cfg.dt0_dirname(tagged), "dt0_D_smooth_0.5_0.5")


class SpeciesSetTests(unittest.TestCase):
    def test_d3dqh_resolves_three_species(self) -> None:
        case = cfg.load_case("d3dqh")
        expected = {
            "e": ("e_f", -1.0, 1),
            "D": ("i_f", 1.0, 2),
            "C": ("i2f", 6.0, 3),
        }
        for name, (var, charge, slot) in expected.items():
            species, tag = case.species_for(name)
            self.assertEqual(tag, name)
            self.assertEqual(species["var_string"], var)
            self.assertEqual(species["charge_num"], charge)
            self.assertEqual(species["slot"], slot)
            # shared keys come from the `species` block
            self.assertTrue(species["has_electron"])

    def test_no_name_keeps_the_single_species_block(self) -> None:
        case = cfg.load_case("cbc6")
        species, tag = case.species_for(None)
        self.assertIsNone(tag)
        self.assertEqual(species, case.species)

    def test_unknown_species_is_refused(self) -> None:
        case = cfg.load_case("d3dqh")
        with self.assertRaises(cfg.CaseError):
            case.species_for("W")


def _fake_case(tmp: str) -> cfg.Case:
    path = Path(tmp) / "t.yaml"
    path.write_text("name: t\nroot: /remote/t\n", encoding="utf-8")
    return cfg.load_case(str(path))


class PlanTests(unittest.TestCase):
    def test_plan_dt0_edits_only_the_expected_parameters(self) -> None:
        files = {"/remote/t/maxwell/input": INPUT, "/remote/t/maxwell/xgcs.cpu": XGCS}
        with tempfile.TemporaryDirectory() as tmp:
            case = _fake_case(tmp)
            with (
                patch.object(proc, "_machine", return_value=({}, "host")),
                patch.object(proc.ssh, "exists", side_effect=lambda _t, p: not p.endswith("/dt0_no_smooth")),
                patch.object(proc.ssh, "read_file", side_effect=lambda _t, p: files[p]),
            ):
                plan = proc.plan_dt0(case, "xgc.f_init_no_smooth.bp")
        self.assertEqual(plan["dir_name"], "dt0_no_smooth")
        self.assertFalse(plan["exists"])
        text = plan["input_text"]
        self.assertIn("sml_dt=2.0D-13", text)
        self.assertIn("sml_mstep=20", text)
        self.assertIn("sml_restart=.false.", text)
        self.assertIn("ptl_input_file_dir='../maxwell/'", text)
        self.assertIn("ptl_f_init_file='xgc.f_init_n570_e.bp' 'xgc.f_init_no_smooth.bp' 'xgc.f_init_n570_C.bp'", text)
        self.assertIn("ptl_maxwellian_init=.true. .false. .true.", text)
        self.assertIn("ptl_nsp=1", text)
        self.assertEqual(plan["resources"]["node_hours"], 4.0)
        self.assertIn("--job-name=t_dt0_no_smooth", plan["script_text"])
        self.assertEqual(len(plan["input_notes"]), 6)

    def test_resolve_finit_from_dt0_input(self) -> None:
        text, _ = nle.apply(INPUT, {
            "ptl_input_file_dir": "'../maxwell/'",
            "ptl_f_init_file": nle.set_slot(nle.read_value(INPUT, "ptl_f_init_file"), 2, "'xgc.f_init_smooth_0.3_0.5.bp'"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            case = _fake_case(tmp)
            path, base = proc.resolve_dt0_finit(case, "dt0_smooth_0.3_0.5", text)
        self.assertEqual(base, "/remote/t/maxwell")
        self.assertEqual(path, "/remote/t/maxwell/xgc.f_init_smooth_0.3_0.5.bp")


class InventoryTests(unittest.TestCase):
    def test_parse_inventory_lines(self) -> None:
        stdout = (
            "maxwell|1|2000|10 20 2000 |xgc.f_init_no_smooth.bp xgc.f_init_smooth_0.3_0.5.bp |1|3\n"
            "dt0_no_smooth|1||10 20 ||1|1\n"
            "dt0_smooth_0_0|1||||0|0\n"
        )
        dirs = proc.parse_inventory(stdout)
        self.assertEqual(dirs[0]["last_f0_step"], 2000)
        self.assertEqual(dirs[0]["twod_steps"], [10, 20, 2000])
        self.assertEqual(len(dirs[0]["f_init_files"]), 2)
        self.assertIsNone(dirs[1]["last_f0_step"])
        self.assertEqual(dirs[1]["finished_jobs"], 1)
        self.assertEqual(dirs[2]["twod_steps"], [])
        # a line written before the 3d fallback has no source column
        self.assertEqual(dirs[0]["density_source"], "xgc.2d")

    def test_parse_inventory_reports_the_density_source(self) -> None:
        stdout = (
            "maxwell|1|9000|7002 7022 |xgc.f_init_D_smooth_0.5_0.5.bp |0|0|xgc.3d\n"
            "dt0_D_smooth_0.5_0.5|1||10 20 ||1|1|xgc.2d\n"
        )
        dirs = proc.parse_inventory(stdout)
        self.assertEqual(dirs[0]["density_source"], "xgc.3d")
        self.assertEqual(dirs[0]["twod_steps"], [7002, 7022])
        self.assertEqual(dirs[1]["density_source"], "xgc.2d")


class LogTests(unittest.TestCase):
    def test_append_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            case = _fake_case(tmp)
            cfg.append_log(case, "submit", dir="dt0_x", job_id="1", state="submitted")
            self.assertTrue(cfg.update_log_entry(case, {"stage": "submit", "job_id": "1"}, state="completed"))
            entries = cfg.read_log(case)
        self.assertEqual(entries[0]["state"], "completed")
        self.assertIn("time", entries[0])


if __name__ == "__main__":
    unittest.main()
