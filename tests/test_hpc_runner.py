from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import hpc_runner as hr
from tools.ssh import CommandResult


class OutputRunDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {"paths": {"run_root": "/scratch/runs"}}

    def test_uses_recorded_case_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            provenance_dir = runs_dir / "run-1"
            provenance_dir.mkdir()
            (provenance_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "machine": "stellar",
                        "remote_run_dir": "/scratch/cases/ecbc-short",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(hr, "RUNS_DIR", runs_dir):
                resolved = hr._output_run_dir("stellar", "run-1", self.cfg)

        self.assertEqual(resolved, "/scratch/cases/ecbc-short")

    def test_uses_classified_provenance_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            provenance_dir = runs_dir / "master" / "run-master"
            provenance_dir.mkdir(parents=True)
            (provenance_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "machine": "stellar",
                        "remote_run_dir": "/scratch/cases/master-case",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(hr, "RUNS_DIR", runs_dir):
                resolved = hr._output_run_dir("stellar", "run-master", self.cfg)

        self.assertEqual(resolved, "/scratch/cases/master-case")

    def test_falls_back_to_run_root_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "RUNS_DIR", Path(tmp)):
                resolved = hr._output_run_dir("stellar", "run-2", self.cfg)

        self.assertEqual(resolved, "/scratch/runs/run-2")

    def test_rejects_machine_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            provenance_dir = runs_dir / "run-3"
            provenance_dir.mkdir()
            (provenance_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "machine": "polaris",
                        "remote_run_dir": "/eagle/project/run-3",
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(hr, "RUNS_DIR", runs_dir):
                with self.assertRaisesRegex(hr.SubmissionError, "belongs to"):
                    hr._output_run_dir("stellar", "run-3", self.cfg)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(hr.SubmissionError, "invalid run_id"):
            hr._output_run_dir("stellar", "../run-4", self.cfg)


class SimulationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "slurm",
                "submit_tpl": "templates/slurm.sbatch.j2",
            },
            "paths": {"run_root": "/scratch/xgc-runs"},
            "xgc": {"exe": "/opt/xgc/bin/xgc", "modules": []},
            "defaults": {
                "account": "project",
                "partition": "all",
                "nodes": 1,
                "walltime": "00:10:00",
            },
        }
        self.record = {
            "simulation_id": "n645st",
            "simulation_number": 645,
            "machines": ["stellar"],
            "machine_codes": ["st"],
            "short_name": "tcv_delta_f",
            "directory_name": "n645st_tcv_delta_f",
            "tags": ["electrostatic"],
        }
        self.directory = {
            "simulation_id": "n645st",
            "machine": "stellar",
            "directory_name": "n645st_tcv_delta_f",
            "remote_directory": "/scratch/xgc-runs/n645st_tcv_delta_f",
            "tags": ["electrostatic"],
        }

    def test_reserved_simulation_uses_canonical_directory(self) -> None:
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(
                hr.sim_registry,
                "simulation_directory",
                return_value=self.directory,
            ),
            patch.object(
                hr.sim_registry,
                "get_simulation",
                return_value=self.record,
            ),
        ):
            plan = hr.prepare_submission(
                "stellar",
                simulation_id="n645st",
                ntasks_per_node=8,
            )

        self.assertEqual(
            plan.remote_run_dir,
            "/scratch/xgc-runs/n645st_tcv_delta_f",
        )
        self.assertEqual(plan.job_name, "xgc-n645st")
        self.assertEqual(plan.provenance_class, "master")
        self.assertEqual(plan.simulation["simulation_number"], 645)
        self.assertIn(
            "#SBATCH --output=/scratch/xgc-runs/n645st_tcv_delta_f/slurm-%j.out",
            plan.script,
        )

    def test_noncanonical_case_directory_is_rejected(self) -> None:
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(
                hr.sim_registry,
                "simulation_directory",
                return_value=self.directory,
            ),
            patch.object(
                hr.sim_registry,
                "get_simulation",
                return_value=self.record,
            ),
        ):
            with self.assertRaisesRegex(hr.SubmissionError, "canonical"):
                hr.prepare_submission(
                    "stellar",
                    simulation_id="n645st",
                    run_dir="/scratch/xgc-runs/wrong-name",
                )

    def test_simulation_id_is_rejected_for_hello_world(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaisesRegex(hr.SubmissionError, "hello-world"):
                hr.prepare_submission(
                    "stellar",
                    simulation_id="n645st",
                    hello_world=True,
                )


class PolarisPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "pbs",
                "submit_tpl": "templates/polaris.pbs.j2",
                "system": "polaris",
                "filesystems": "home:grand",
            },
            "paths": {"run_root": "/grand/SomeProject/alice/xgc-runs"},
            "xgc": {
                "exe": "/grand/SomeProject/alice/XGC-Devel/build/bin/xgc",
                "gpu_aware_mpi": False,
                "env_script": (
                    "/grand/SomeProject/alice/XGC-Devel/"
                    "quickstart/modules_and_env/polaris.sh"
                ),
                "modules": [],
            },
            "defaults": {
                "account": "SomeProject",
                "queue": "debug-scaling",
                "nodes": 4,
                "ntasks_per_node": 4,
                "cpus_per_task": 16,
                "gpus_per_node": 4,
                "walltime": "01:00:00",
            },
        }

    def test_polaris_template_renders_required_resources_and_affinity(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "polaris",
                run_id="run-polaris-test",
            )

        directives = {
            line for line in plan.script.splitlines() if line.startswith("#PBS")
        }
        self.assertIn("#PBS -l select=4:system=polaris", directives)
        self.assertIn("#PBS -l place=scatter", directives)
        self.assertIn("#PBS -l filesystems=home:grand", directives)
        self.assertIn("#PBS -q debug-scaling", directives)
        self.assertIn("#PBS -A SomeProject", directives)
        self.assertIn("mpiexec --np 16 -ppn 4", plan.script)
        self.assertIn("CUDA_VISIBLE_DEVICES", plan.script)
        self.assertIn(
            "gpu=$((4 - 1 - local_rank % 4))",
            plan.script,
        )
        self.assertNotIn("MPICH_GPU_SUPPORT_ENABLED", plan.script)
        self.assertIn("pbs-${PBS_JOBID}.out", plan.script)
        self.assertEqual(plan.resources["total_ranks"], 16)
        self.assertEqual(plan.resources["cpus_per_task"], 16)
        self.assertEqual(plan.resources["gpus_per_node"], 4)
        self.assertEqual(plan.resources["filesystems"], "home:grand")
        self.assertEqual(plan.resources["node_hours_max"], 4.0)

    def test_polaris_enables_gpu_aware_mpi_only_when_configured(self) -> None:
        self.cfg["xgc"]["gpu_aware_mpi"] = True
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "polaris",
                run_id="run-polaris-gpu-aware-mpi",
            )

        self.assertIn("export MPICH_GPU_SUPPORT_ENABLED=1", plan.script)

    def test_polaris_hello_world_uses_mpiexec_without_gpu_wrapper(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "polaris",
                run_id="run-polaris-hello",
                hello_world=True,
            )

        self.assertIn("mpiexec --np 16 -ppn 4 hostname", plan.script)
        self.assertNotIn("xgc-rank", plan.script)

    def test_polaris_plan_rejects_missing_filesystem_resource(self) -> None:
        self.cfg["scheduler"]["filesystems"] = None
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaisesRegex(hr.SubmissionError, "filesystems"):
                hr.prepare_submission(
                    "polaris",
                    run_id="run-polaris-missing-filesystem",
                    hello_world=True,
                )

    def test_pbs_status_reports_exit_code_and_failure(self) -> None:
        output = (
            "Job Id: 12345.polaris-pbs-01\n"
            "    job_state = F\n"
            "    Exit_status = 9\n"
        )
        remote_result = CommandResult(
            returncode=0,
            stdout=output,
            stderr="",
            command="qstat -x -f 12345.polaris-pbs-01",
        )
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="polaris"),
            patch.object(hr.ssh, "run", return_value=remote_result),
        ):
            result = hr.status("polaris", "12345.polaris-pbs-01")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], "9")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["source"], "pbs")


class AuroraPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "pbs",
                "submit_tpl": "templates/aurora.pbs.j2",
                "system": "aurora",
                "filesystems": "home:flare",
            },
            "paths": {"run_root": "/lus/flare/projects/SomeProject/alice"},
            "xgc": {
                "exe": "/lus/flare/projects/SomeProject/alice/XGC-Devel/build/bin/xgc",
                "gpu_aware_mpi": True,
                "env_script": (
                    "/lus/flare/projects/SomeProject/alice/XGC-Devel/"
                    "quickstart/modules_and_env/aurora.sh"
                ),
                "modules": [],
            },
            "defaults": {
                "account": "SomeProject",
                "queue": "debug",
                "nodes": 1,
                "ntasks_per_node": 12,
                "cpus_per_task": 16,
                "gpus_per_node": 6,
                "walltime": "00:30:00",
            },
        }

    def test_aurora_template_binds_one_rank_per_gpu_tile(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission("aurora", run_id="run-aurora-test")

        directives = {
            line for line in plan.script.splitlines() if line.startswith("#PBS")
        }
        # qsub on Aurora rejects a system= chunk resource (unlike Polaris).
        self.assertIn("#PBS -l select=1", directives)
        self.assertFalse(any("system=" in line for line in directives))
        self.assertIn("#PBS -l place=scatter", directives)
        self.assertIn("#PBS -l filesystems=home:flare", directives)
        self.assertIn("#PBS -q debug", directives)
        self.assertIn("#PBS -A SomeProject", directives)
        self.assertIn("mpiexec --np 12 -ppn 12", plan.script)
        # Twelve explicit CPU groups and twelve GPU tiles, one per rank.
        self.assertIn("--cpu-bind=list:1-8,105-112:", plan.script)
        self.assertIn("--gpu-bind=list:0.0:0.1:1.0:1.1:2.0:2.1:3.0:3.1:4.0:4.1:5.0:5.1", plan.script)
        self.assertIn("export MPIR_CVAR_ENABLE_GPU=1", plan.script)
        self.assertIn("ZE_FLAT_DEVICE_HIERARCHY=COMPOSITE", plan.script)
        self.assertNotIn("CUDA_VISIBLE_DEVICES", plan.script)
        self.assertNotIn("MPICH_GPU_SUPPORT_ENABLED", plan.script)
        self.assertIn("pbs-${PBS_JOBID}.out", plan.script)
        self.assertEqual(plan.resources["total_ranks"], 12)
        self.assertEqual(plan.resources["node_hours_max"], 0.5)

    def test_aurora_other_rank_counts_use_tile_compact_wrapper(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "aurora", run_id="run-aurora-6", ntasks_per_node=6
            )

        launch = [
            line for line in plan.script.splitlines() if not line.startswith("#")
        ]
        self.assertIn("mpiexec --np 6 -ppn 6 \\", launch)
        self.assertTrue(any("gpu_tile_compact.sh" in line for line in launch))
        self.assertFalse(any("--gpu-bind=list" in line for line in launch))

    def test_aurora_disables_gpu_aware_mpi_when_configured(self) -> None:
        self.cfg["xgc"]["gpu_aware_mpi"] = False
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission("aurora", run_id="run-aurora-nogpu")

        self.assertIn("export MPIR_CVAR_ENABLE_GPU=0", plan.script)

    def test_aurora_hello_world_uses_plain_mpiexec(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "aurora", run_id="run-aurora-hello", hello_world=True
            )

        launch = [
            line for line in plan.script.splitlines() if not line.startswith("#")
        ]
        self.assertIn("mpiexec --np 12 -ppn 12 hostname", launch)
        self.assertFalse(any("gpu-bind" in line for line in launch))


class PerlmutterPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "slurm",
                "submit_tpl": "templates/slurm.sbatch.j2",
            },
            "paths": {"run_root": "/pscratch/sd/a/alice/xgc-runs"},
            "xgc": {"exe": None, "modules": []},
            "defaults": {
                "account": "m0000",
                "qos": "debug",
                "constraint": "cpu",
                "nodes": 1,
                "ntasks_per_node": 32,
                "cpus_per_task": 8,
                "walltime": "00:30:00",
                "mail_type": "BEGIN,END,FAIL",
                "mail_user": "alice@example.org",
            },
        }

    def test_hello_world_uses_shared_slurm_template_and_mail_notifications(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-access-check",
                hello_world=True,
            )

        self.assertIn("#SBATCH --account=m0000", plan.script)
        self.assertIn("#SBATCH --qos=debug", plan.script)
        self.assertIn("#SBATCH --constraint=cpu", plan.script)
        self.assertIn("#SBATCH --mail-type=BEGIN,END,FAIL", plan.script)
        self.assertIn("#SBATCH --mail-user=alice@example.org", plan.script)
        self.assertIn("srun -n 32 hostname", plan.script)
        self.assertEqual(plan.resources["mail_user"], "alice@example.org")
        self.assertEqual(plan.provenance_class, "test")

    def test_build_overrides_are_rendered_and_recorded(self) -> None:
        executable = "/pscratch/builds/xgca/bin/xgca"
        env_script = "/pscratch/builds/xgca/setup.sh"
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-observed-build",
                xgc_exe=executable,
                env_script=env_script,
            )

        self.assertIn(f"source {env_script}", plan.script)
        self.assertIn(f"srun -n 32 {executable}", plan.script)
        self.assertEqual(plan.resources["executable"], executable)
        self.assertEqual(plan.resources["env_script"], env_script)

    def test_prebuilt_plan_passes_its_build_to_dryrun(self) -> None:
        executable = "/pscratch/builds/xgca/bin/xgca"
        env_script = "/pscratch/builds/xgca/setup.sh"
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-dryrun-build",
                xgc_exe=executable,
                env_script=env_script,
            )

        rejected = hr.dryrun.DryrunReport(ok=False, returncode=1)
        with patch.object(hr.dryrun, "run_dryrun", return_value=rejected) as run:
            result = hr.submit(
                "perlmutter",
                confirm=True,
                plan=plan,
                case_dir="/pscratch/cases/tcv",
            )

        self.assertFalse(result["submitted"])
        run.assert_called_once_with(
            "perlmutter",
            "/pscratch/cases/tcv",
            n_ranks=32,
            xgc_exe=executable,
            env_script=env_script,
        )

    def test_provenance_is_written_below_plan_class(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-test-record",
                xgc_exe="/pscratch/builds/xgca/bin/xgca",
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "RUNS_DIR", Path(tmp)):
                path = hr._write_provenance(plan, {"run_id": plan.run_id})
                expected = (
                    Path(tmp)
                    / "test"
                    / "run-perlmutter-test-record"
                    / "provenance.json"
                )
                self.assertEqual(path, expected)
                self.assertTrue(expected.is_file())

    def test_input_snapshot_is_stored_next_to_the_record(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-input-copy",
                xgc_exe="/pscratch/builds/xgca/bin/xgca",
            )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "RUNS_DIR", Path(tmp)):
                path = hr._write_provenance(
                    plan, {"run_id": plan.run_id}, input_text="&sml_param\n/\n"
                )
                self.assertEqual(
                    (path.parent / "input").read_text(encoding="utf-8"),
                    "&sml_param\n/\n",
                )


class InputCaptureTests(unittest.TestCase):
    """The submitted namelist must be hashed, since the remote copy gets edited."""

    cfg = {"paths": {"run_root": "/scratch/runs"}}

    def test_input_is_read_and_hashed(self) -> None:
        result = CommandResult(0, "&sml_param\n  sml_dt = 1.0\n/\n", "", "cat")
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="perlmutter"),
            patch.object(hr.ssh, "run", return_value=result) as run,
        ):
            captured = hr._capture_input("perlmutter", "/pscratch/cases/tcv/")

        run.assert_called_once_with("perlmutter", ["cat", "/pscratch/cases/tcv/input"])
        self.assertEqual(captured["input_capture"], "ok")
        self.assertEqual(captured["input_text"], result.stdout)
        self.assertEqual(len(captured["input_sha256"]), 64)

    def test_identical_inputs_hash_identically(self) -> None:
        result = CommandResult(0, "&sml_param\n/\n", "", "cat")
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="perlmutter"),
            patch.object(hr.ssh, "run", return_value=result),
        ):
            first = hr._capture_input("perlmutter", "/a")
            second = hr._capture_input("perlmutter", "/b")
        self.assertEqual(first["input_sha256"], second["input_sha256"])

    def test_unreadable_input_is_reported_not_raised(self) -> None:
        # A capture failure must never turn an accepted submission into an
        # apparent error; the job is already queued by this point.
        result = CommandResult(1, "", "No such file", "cat")
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="perlmutter"),
            patch.object(hr.ssh, "run", return_value=result),
        ):
            captured = hr._capture_input("perlmutter", "/pscratch/cases/tcv")
        self.assertIsNone(captured["input_sha256"])
        self.assertIn("cannot read", captured["input_capture"])

    def test_hello_world_submission_has_no_input_to_capture(self) -> None:
        captured = hr._capture_input("perlmutter", None)
        self.assertIsNone(captured["input_sha256"])
        self.assertEqual(captured["input_capture"], "no case_dir")

    def test_input_provenance_uses_xgc_namelist_parser(self) -> None:
        parsed = {
            "sml_param": {
                "sml_node_file": "'mesh.node'",
                "sml_electron_on": ".true.",
            },
            "eq_param": {"eq_filename": "'equilibrium.eqd'"},
            "ptl_param": {"ptl_marker_type": "'ReducedDeltaF' 'ReducedDeltaF'"},
        }
        with patch.object(hr.nlm, "read_namelist", return_value=parsed) as read:
            result = hr._input_provenance("perlmutter", "/pscratch/cases/tcv/")

        read.assert_called_once_with("perlmutter", "/pscratch/cases/tcv/input")
        self.assertEqual(result["parameters"]["sml_node_file"], "mesh.node")
        self.assertEqual(result["derived"]["species"], ["e", "i"])
        self.assertEqual(result["derived"]["field_model"], "unknown")

    def test_namelist_parser_failure_is_recorded_as_unknown(self) -> None:
        with patch.object(
            hr.nlm, "read_namelist", side_effect=hr.nlm.NamelistError("old schema")
        ):
            result = hr._input_provenance("perlmutter", "/pscratch/cases/old")

        self.assertEqual(result["parse_status"], "unavailable")
        self.assertEqual(result["derived"]["field_model"], "unknown")
        self.assertEqual(result["derived"]["marker_model"], "unknown")
        self.assertIn("old schema", result["parse_error"])

    def test_current_dryrun_supplies_effective_field_model(self) -> None:
        parsed = {"sml_param": {"sml_electron_on": ".true."}}
        dryrun = {"parallelism": {"field_model": "electrostatic"}}
        with patch.object(hr.nlm, "read_namelist", return_value=parsed):
            result = hr._input_provenance(
                "perlmutter", "/pscratch/cases/current", dryrun_report=dryrun
            )

        self.assertEqual(result["derived"]["field_model"], "electrostatic")
        self.assertEqual(result["derived"]["field_model_source"], "xgc --dryrun")


class CatalogLinkTests(unittest.TestCase):
    """Submitting and checking a job must maintain the simulation's record."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.catalog_dir = self.root / "simulations"
        self.catalog_patch = patch.object(
            hr.catalog, "CATALOG_DIR", self.catalog_dir
        )
        self.catalog_patch.start()
        self.runs_patch = patch.object(hr, "RUNS_DIR", self.runs)
        self.runs_patch.start()
        hr.catalog.create_record(
            "n645pe", machines=["perlmutter"], short_name="tcv delta f"
        )

    def tearDown(self) -> None:
        self.runs_patch.stop()
        self.catalog_patch.stop()
        self.tmp.cleanup()

    def _record(self, **overrides: object) -> dict:
        record = {
            "run_id": "run-20260830T101010Z",
            "machine": "perlmutter",
            "job_id": "12345",
            "submitted_at": "2026-08-30T10:10:10+00:00",
            "remote_run_dir": "/pscratch/x/n645pe_tcv_delta_f",
            "simulation_id": "n645pe",
            "simulation": {"simulation_id": "n645pe"},
            "input_sha256": "deadbeef",
        }
        record.update(overrides)
        return record

    def _write_provenance_record(self, record: dict) -> Path:
        path = self.runs / "master" / record["run_id"] / "provenance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_submission_links_the_run_to_its_simulation(self) -> None:
        record = self._record()
        path = self._write_provenance_record(record)

        link = hr._link_catalog(record, path)

        self.assertEqual(link, {"simulation_id": "n645pe", "linked": True})
        stored = hr.catalog.read_record("n645pe")
        self.assertEqual(stored["status"], "submitted")
        self.assertEqual(stored["runs"][0]["job_id"], "12345")
        self.assertEqual(stored["input_sha256"], "deadbeef")
        self.assertEqual(
            stored["remote_dirs"]["perlmutter"], "/pscratch/x/n645pe_tcv_delta_f"
        )

    def test_unregistered_run_has_no_catalog_to_link(self) -> None:
        record = self._record(simulation_id=None, simulation=None)
        path = self._write_provenance_record(record)
        self.assertIsNone(hr._link_catalog(record, path))

    def test_catalog_failure_is_reported_rather_than_raised(self) -> None:
        # The job is already queued; losing the link must not look like a
        # failed submission.
        record = self._record(simulation_id="n999zz", simulation={"simulation_id": "n999zz"})
        path = self._write_provenance_record(record)

        link = hr._link_catalog(record, path)

        self.assertFalse(link["linked"])
        self.assertIn("n999zz", link["error"])

    def test_status_check_maps_a_job_back_to_its_simulation(self) -> None:
        record = self._record()
        path = self._write_provenance_record(record)
        hr._link_catalog(record, path)

        found = hr._find_run_for_job("perlmutter", "12345")

        self.assertEqual(found, ("n645pe", "run-20260830T101010Z"))

    def test_job_id_from_another_machine_is_not_matched(self) -> None:
        record = self._record()
        self._write_provenance_record(record)
        self.assertIsNone(hr._find_run_for_job("polaris", "12345"))

    def test_terminal_status_advances_the_simulation(self) -> None:
        record = self._record()
        path = self._write_provenance_record(record)
        hr._link_catalog(record, path)

        update = hr._record_observed_state(
            "perlmutter",
            "12345",
            {"state": "completed", "exit_code": 0, "terminal": True},
        )

        self.assertTrue(update["updated"])
        stored = hr.catalog.read_record("n645pe")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["runs"][0]["exit_code"], 0)

    def test_status_of_an_unknown_job_changes_nothing(self) -> None:
        self.assertIsNone(
            hr._record_observed_state(
                "perlmutter", "99999", {"state": "completed", "terminal": True}
            )
        )


class SubmitToStatusLifecycleTests(unittest.TestCase):
    """Submit a registered simulation and follow it through to completion.

    The individual pieces are covered above; this checks that they actually
    compose, which is the whole promise of the catalog: a number, a job, a
    directory, and an input hash stay attached without anyone recording them.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {
            "scheduler": {
                "type": "slurm",
                "submit": "sbatch",
                "submit_tpl": "templates/slurm.sbatch.j2",
            },
            "paths": {"run_root": "/scratch/xgc-runs"},
            "xgc": {"exe": "/opt/xgc/bin/xgc", "modules": []},
            "defaults": {
                "account": "proj",
                "partition": "all",
                "nodes": 1,
                "walltime": "00:10:00",
            },
        }
        self.simulation = {
            "simulation_id": "n645st",
            "simulation_number": 645,
            "machines": ["stellar"],
            "machine_codes": ["st"],
            "short_name": "tcv",
            "directory_name": "n645st_tcv",
            "tags": [],
        }
        self.directory = {
            "simulation_id": "n645st",
            "machine": "stellar",
            "directory_name": "n645st_tcv",
            "remote_directory": "/scratch/xgc-runs/n645st_tcv",
            "tags": [],
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _remote(host: str, argv: list[str], **kwargs: object) -> CommandResult:
        if argv[0] == "sbatch":
            return CommandResult(0, "Submitted batch job 777", "", "sbatch")
        if argv[0] == "cat":
            return CommandResult(0, "&sml_param\n  sml_mstep = 200\n/\n", "", "cat")
        return CommandResult(0, "", "", " ".join(argv))

    def test_submitting_then_checking_a_job_maintains_the_record(self) -> None:
        accepted = hr.dryrun.DryrunReport(ok=True, returncode=0)
        with (
            patch.object(hr, "RUNS_DIR", self.root / "runs"),
            patch.object(hr.catalog, "CATALOG_DIR", self.root / "simulations"),
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(
                hr.sim_registry, "simulation_directory", return_value=self.directory
            ),
            patch.object(
                hr.sim_registry, "get_simulation", return_value=self.simulation
            ),
            patch.object(hr.ssh, "host_for", return_value="stellar"),
            patch.object(hr.ssh, "mkdir_p"),
            patch.object(hr.ssh, "write_file"),
            patch.object(hr.dryrun, "run_dryrun", return_value=accepted),
            patch.object(
                hr.nlm,
                "read_namelist",
                return_value={
                    "sml_param": {
                        "sml_node_file": "'stellar.node'",
                        "sml_electron_on": ".false.",
                    },
                    "eq_param": {"eq_filename": "'stellar.eqd'"},
                    "ptl_param": {"ptl_marker_type": "'TotalF'"},
                },
            ),
        ):
            hr.catalog.create_record("n645st", machines=["stellar"], short_name="tcv")

            with patch.object(hr.ssh, "run", side_effect=self._remote):
                result = hr.submit(
                    "stellar",
                    confirm=True,
                    simulation_id="n645st",
                    case_dir="/scratch/xgc-runs/n645st_tcv",
                )

            self.assertTrue(result["submitted"])
            self.assertEqual(result["catalog"], {"simulation_id": "n645st", "linked": True})
            record = hr.catalog.read_record("n645st")
            self.assertEqual(record["status"], "submitted")
            self.assertEqual(record["runs"][0]["job_id"], "777")
            self.assertEqual(record["input_sha256"], result["input_sha256"])
            snapshot = self.root / "runs" / "master" / result["run_id"] / "input"
            self.assertIn("sml_mstep", snapshot.read_text(encoding="utf-8"))
            provenance = json.loads(
                (snapshot.parent / "provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                provenance["input"]["parameters"]["sml_node_file"], "stellar.node"
            )
            self.assertEqual(provenance["input"]["derived"]["species"], ["i"])
            self.assertEqual(provenance["input"]["derived"]["marker_model"], "total-f")

            finished = CommandResult(0, "COMPLETED 0:0", "", "squeue")
            with patch.object(hr.ssh, "run", return_value=finished):
                status = hr.status("stellar", "777")

            self.assertEqual(status["state"], "completed")
            self.assertTrue(status["catalog"]["updated"])
            self.assertEqual(hr.catalog.read_record("n645st")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
