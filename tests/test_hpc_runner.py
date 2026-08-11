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


if __name__ == "__main__":
    unittest.main()
