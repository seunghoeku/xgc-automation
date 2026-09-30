from __future__ import annotations

import json
import tempfile
import subprocess
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

    def test_polaris_spare_nodes_add_a_gpu_gate_and_grow_the_allocation(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "polaris",
                run_id="run-polaris-spare",
                nodes=32,
                spare_nodes=2,
                walltime="06:00:00",
            )

        directives = {
            line for line in plan.script.splitlines() if line.startswith("#PBS")
        }
        # The allocation grows by the spares; the rank count does not.
        self.assertIn("#PBS -l select=34:system=polaris", directives)
        self.assertEqual(plan.resources["nodes"], 34)
        self.assertEqual(plan.resources["compute_nodes"], 32)
        self.assertEqual(plan.resources["spare_nodes"], 2)
        self.assertEqual(plan.resources["total_ranks"], 128)
        self.assertEqual(plan.resources["node_hours_max"], 204.0)
        # XGC launches from a probed hostfile, never from $PBS_NODEFILE directly.
        self.assertIn('mpiexec --hostfile "$GOOD_HOSTS" --np 128 -ppn 4', plan.script)
        self.assertIn("GPU gate", plan.script)
        # The probe checks more than the device list: busy/prohibited GPUs and
        # a CUDA primary-context open, the step that failed in n674po 7651746.
        self.assertIn("--query-gpu=index,uuid,compute_mode,pstate", plan.script)
        self.assertIn("--query-compute-apps=gpu_uuid,pid", plan.script)
        self.assertIn("cuDevicePrimaryCtxRetain", plan.script)
        self.assertIn("nvidia-smi -L", plan.script)  # fallback without python3
        # Relaunches repeat while enough healthy nodes remain.
        self.assertIn("while true; do", plan.script)
        self.assertIn("CUDA_VISIBLE_DEVICES", plan.script)
        self.assertNotIn("mpiexec --np 128 -ppn 4 \\", plan.script)
        # The rendered script must be valid bash.
        check = subprocess.run(
            ["bash", "-n"], input=plan.script, text=True, capture_output=True
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_known_bad_hosts_expire_after_the_window_or_when_cleared(self) -> None:
        from datetime import date

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad_nodes.yaml"
            path.write_text(
                "polaris:\n"
                "  x1000c0s0b0n0:\n"  # failed 2 days ago: excluded
                "    failures: [{date: 2026-09-18, job: '0'}, {date: 2026-09-23, job: '1'}]\n"
                "  x2000c0s0b0n0.hsn.cm.polaris.alcf.anl.gov:\n"  # 6 days: excluded
                "    failures: [{date: 2026-09-19, job: '2'}]\n"
                "  x3000c0s0b0n0:\n"  # 7 days: window over, probed again
                "    failures: [{date: 2026-09-18, job: '3'}]\n"
                "  x4000c0s0b0n0:\n"  # recent but cleared afterwards
                "    cleared: 2026-09-24\n"
                "    failures: [{date: 2026-09-23, job: '4'}]\n"
                "  x5000c0s0b0n0:\n"  # failed again after being cleared
                "    cleared: 2026-09-20\n"
                "    failures: [{date: 2026-09-24, job: '5'}]\n",
                encoding="utf-8",
            )
            today = date(2026, 9, 25)
            self.assertEqual(
                hr.known_bad_hosts("polaris", path, today=today),
                ["x1000c0s0b0n0", "x2000c0s0b0n0", "x5000c0s0b0n0"],
            )
            self.assertEqual(hr.known_bad_hosts("aurora", path, today=today), [])
            self.assertEqual(hr.known_bad_hosts("polaris", Path(tmp) / "none"), [])
            # A top-level exclude_days overrides the default window.
            path.write_text(
                "exclude_days: 3\npolaris:\n  x2000c0s0b0n0:\n"
                "    failures: [{date: 2026-09-19}]\n",
                encoding="utf-8",
            )
            self.assertEqual(hr.known_bad_hosts("polaris", path, today=today), [])
        # The committed list: the n674po nodes are excluded right after 7651746
        # and released a week later.
        self.assertIn(
            "x3208c0s37b1n0", hr.known_bad_hosts("polaris", today=date(2026, 9, 25))
        )
        self.assertEqual(hr.known_bad_hosts("polaris", today=date(2026, 10, 3)), [])

    def test_known_bad_hosts_are_excluded_only_with_spare_nodes(self) -> None:
        with patch.object(hr, "known_bad_hosts", return_value=["x9c0s1b0n0"]):
            with patch.object(hr.m, "get_machine", return_value=self.cfg):
                gated = hr.prepare_submission(
                    "polaris", run_id="run-polaris-excl", spare_nodes=1
                )
                plain = hr.prepare_submission("polaris", run_id="run-polaris-noexcl")
        self.assertIn('KNOWN_BAD_HOSTS="x9c0s1b0n0"', gated.script)
        self.assertEqual(gated.resources["excluded_hosts"], ["x9c0s1b0n0"])
        self.assertEqual(plain.resources["excluded_hosts"], [])

    def _run_gate(self, tmp: Path, *, nodes: int, spare: int, hosts: list[str],
                  known_bad: list[str], probe_bad: list[str],
                  crashes: list[str]) -> tuple[subprocess.CompletedProcess, Path]:
        """Run the rendered gate with stub mpiexec/timeout/date on PATH.

        The stub mpiexec answers the probe (``probe_bad`` hosts report 3 of 4
        GPUs usable) and, for launch N, crashes on ``crashes[N-1]`` with a
        signal-6 rank there plus a signal-15 teardown rank on another node.
        """
        run_dir = tmp / "case"
        run_dir.mkdir()
        stub = tmp / "stub"
        stub.mkdir()
        fqdn = [f"{h}.hsn.cm.polaris.alcf.anl.gov" for h in hosts]
        (tmp / "nodefile").write_text("\n".join(fqdn) + "\n", encoding="utf-8")
        (stub / "probe_bad").write_text("".join(f"{h}\n" for h in probe_bad))
        for i, host in enumerate(crashes, start=1):
            (stub / f"crash{i}").write_text(host)
        scripts = {
            "timeout": '#!/bin/bash\nshift\nexec "$@"\n',
            "date": (
                '#!/bin/bash\n'
                'if [ "${1:-}" = "-Is" ]; then echo 2026-09-25T00:00:00; '
                'else exec /bin/date "$@"; fi\n'
            ),
            "mpiexec": f"""#!/bin/bash
stub="{stub}"
while [ $# -gt 0 ]; do
  case "$1" in --hostfile) hostfile="$2"; shift 2 ;; *) args+=("$1"); shift ;; esac
done
if [[ " ${{args[*]}} " == *" gpu-probe "* ]]; then
  while read -r h; do
    if grep -q -x -F "${{h%%.*}}" "$stub/probe_bad"; then echo "$h 3 4 gpu2:context-error-46"
    else echo "$h 4 4 ok"; fi
  done <"$hostfile"
  exit 0
fi
n=$(( $(cat "$stub/launches" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$stub/launches"
cp "$hostfile" "$stub/launch$n.hosts"
if [ -f "$stub/crash$n" ]; then
  bad=$(cat "$stub/crash$n")
  other=$(grep -v "^$bad" "$hostfile" | head -1)
  echo "$bad.hsn.cm.polaris.alcf.anl.gov: rank 5 died from signal 6 and dumped core" >&2
  echo "$other: rank 9 died from signal 15" >&2
  exit 134
fi
exit 0
""",
        }
        for name, body in scripts.items():
            (stub / name).write_text(body)
            (stub / name).chmod(0o755)
        cfg = dict(self.cfg, xgc=dict(self.cfg["xgc"], env_script=None))
        with patch.object(hr, "known_bad_hosts", return_value=known_bad):
            with patch.object(hr.m, "get_machine", return_value=cfg):
                plan = hr.prepare_submission(
                    "polaris", run_id="run-gate-sim", nodes=nodes,
                    spare_nodes=spare, run_dir=str(run_dir),
                )
        env = {
            "PATH": f"{stub}:/usr/bin:/bin",
            "PBS_JOBID": "1.test",
            "PBS_NODEFILE": str(tmp / "nodefile"),
            "HOME": str(tmp),
        }
        result = subprocess.run(
            ["bash", "-c", plan.script], env=env, text=True, capture_output=True,
            timeout=60,
        )
        return result, run_dir

    def test_gpu_gate_keeps_relaunching_while_enough_nodes_remain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            result, run_dir = self._run_gate(
                tmp, nodes=2, spare=4,
                hosts=["n1", "n2", "n3", "n4", "n5", "n6"],
                known_bad=["n1"], probe_bad=["n2"], crashes=["n3", "n5"],
            )
            out = (run_dir / "pbs-1.test.out").read_text()
            launches = int((tmp / "stub" / "launches").read_text())
            last_hosts = (tmp / "stub" / f"launch{launches}.hosts").read_text().split()
            bad = (run_dir / "gpu_gate.1.test.bad_hosts").read_text()

        self.assertEqual(result.returncode, 0, out + result.stderr)
        # Two crashes, two relaunches: more than the old single retry.
        self.assertEqual(launches, 3)
        self.assertIn("GPU gate: launch 3 rc=0", out)
        # n1 never probed (known bad), n2 failed the probe, n3/n5 crashed; the
        # signal-15 teardown rank on n4 did not get n4 dropped.
        self.assertEqual([h.split(".")[0] for h in last_hosts], ["n4", "n6"])
        self.assertIn("n1.hsn.cm.polaris.alcf.anl.gov known-bad", bad)
        self.assertIn("n2.hsn.cm.polaris.alcf.anl.gov 3-of-4-gpus-usable gpu2:context-error-46", bad)
        self.assertIn("n3.hsn.cm.polaris.alcf.anl.gov crashed-in-launch-1", bad)
        self.assertIn("n5.hsn.cm.polaris.alcf.anl.gov crashed-in-launch-2", bad)
        self.assertNotIn("n4", bad)

    def test_gpu_gate_stops_when_too_few_healthy_nodes_remain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            result, run_dir = self._run_gate(
                tmp, nodes=2, spare=1, hosts=["n1", "n2", "n3"],
                known_bad=[], probe_bad=[], crashes=["n1", "n2"],
            )
            out = (run_dir / "pbs-1.test.out").read_text()
            launches = int((tmp / "stub" / "launches").read_text())

        self.assertEqual(result.returncode, 134, out + result.stderr)
        self.assertEqual(launches, 2)
        self.assertIn("not enough healthy nodes left for a relaunch", out)
        self.assertIn("end=2026-09-25T00:00:00 rc=134", out)

    def test_depend_on_renders_an_afterany_pbs_directive(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "polaris", run_id="run-polaris-chain", depend_on="7628761.polaris-pbs-01"
            )
        directives = [
            line for line in plan.script.splitlines() if line.startswith("#PBS")
        ]
        self.assertIn("#PBS -W depend=afterany:7628761.polaris-pbs-01", directives)
        # Directives stay contiguous at the top: the dependency comes before -k.
        self.assertLess(
            directives.index("#PBS -W depend=afterany:7628761.polaris-pbs-01"),
            directives.index("#PBS -k doe"),
        )
        self.assertEqual(plan.resources["depend_on"], "7628761.polaris-pbs-01")

    def test_depend_on_rejects_anything_but_one_job_id(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            for bad in ("afterany:123", "123 456", "abc"):
                with self.assertRaises(hr.SubmissionError):
                    hr.prepare_submission("polaris", run_id="run-bad-dep", depend_on=bad)
            plan = hr.prepare_submission("polaris", run_id="run-no-dep", depend_on="  ")
        self.assertIsNone(plan.resources["depend_on"])
        self.assertNotIn("depend=", plan.script)

    def test_polaris_without_spare_nodes_keeps_the_plain_launcher(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission("polaris", run_id="run-polaris-plain")

        self.assertNotIn("GPU gate", plan.script)
        self.assertNotIn("--hostfile", plan.script)
        self.assertEqual(plan.resources["spare_nodes"], 0)

    def test_spare_nodes_need_gpus_and_a_supporting_template(self) -> None:
        self.cfg["defaults"]["gpus_per_node"] = None
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaises(hr.SubmissionError):
                hr.prepare_submission(
                    "polaris", run_id="run-polaris-spare-nogpu", spare_nodes=1
                )

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

    def test_aurora_daos_runs_inside_the_container_and_keeps_logs_on_flare(self) -> None:
        self.cfg["daos"] = {"pool": "SomeProject", "container": "xgc"}
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission(
                "aurora",
                run_id="run-aurora-daos",
                run_dir="/lus/flare/projects/SomeProject/alice/case_a",
                daos=True,
            )

        launch = [
            line for line in plan.script.splitlines() if not line.startswith("#")
        ]
        directives = {line for line in launch if line.startswith("#PBS")}
        self.assertEqual(plan.resources["filesystems"], "home:flare:daos_user_fs")
        self.assertIn(
            "/tmp/SomeProject/xgc/case_a", plan.resources["daos"]["work_dir"]
        )
        self.assertTrue(any("launch-dfuse.sh SomeProject:xgc" in l for l in launch))
        self.assertTrue(any('cd "/tmp/SomeProject/xgc/case_a"' in l for l in launch))
        self.assertTrue(
            any("--no-vni --env LD_PRELOAD=/usr/lib64/libpil4dfs.so" in l for l in launch)
        )
        # Logs stay on the parallel filesystem where the login node can read them.
        self.assertTrue(
            any('exec >"/lus/flare/projects/SomeProject/alice/case_a/pbs-' in l for l in launch)
        )
        self.assertEqual(plan.remote_run_dir, "/lus/flare/projects/SomeProject/alice/case_a")

    def test_aurora_daos_requires_pool_and_container(self) -> None:
        self.cfg["daos"] = {"pool": "<daos_pool>", "container": "xgc"}
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaisesRegex(hr.SubmissionError, "daos.pool"):
                hr.prepare_submission("aurora", run_id="run-daos-missing", daos=True)
        del self.cfg["daos"]
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            with self.assertRaisesRegex(hr.SubmissionError, "no daos section"):
                hr.prepare_submission("aurora", run_id="run-daos-none", daos=True)

    def test_aurora_without_daos_is_unchanged(self) -> None:
        self.cfg["daos"] = {"pool": "SomeProject", "container": "xgc"}
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            plan = hr.prepare_submission("aurora", run_id="run-aurora-plain")
        self.assertNotIn("dfuse", plan.script)
        self.assertNotIn("--no-vni", plan.script)
        self.assertEqual(plan.resources["filesystems"], "home:flare")
        self.assertIsNone(plan.resources["daos"])

    def test_daos_submit_blocks_a_relative_input_file_dir(self) -> None:
        self.cfg["daos"] = {"pool": "SomeProject", "container": "xgc"}
        case = "/lus/flare/projects/SomeProject/alice/case_a"
        remote_input = CommandResult(
            0, "&sml_param\nsml_input_file_dir='../XGC_inputs/'\n/\n", "", "cat"
        )
        with (
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="aurora"),
            patch.object(hr.ssh, "run", return_value=remote_input) as run,
            patch.object(hr.ssh, "mkdir_p") as mkdir,
        ):
            plan = hr.prepare_submission(
                "aurora", run_id="run-daos-relative", run_dir=case, daos=True
            )
            result = hr.submit(
                "aurora", confirm=True, plan=plan, case_dir=case, skip_dryrun=True
            )

        self.assertFalse(result["submitted"])
        self.assertIn("relative", result["reason"])
        mkdir.assert_not_called()
        self.assertEqual(run.call_count, 1)


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

    def test_one_gpu_per_rank_when_ranks_match_gpus(self) -> None:
        with patch.object(hr.m, "get_machine", return_value=self.cfg):
            matched = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-gpu-bind",
                hello_world=True,
                ntasks_per_node=4,
                gpus_per_node=4,
            )
            unmatched = hr.prepare_submission(
                "perlmutter",
                run_id="run-perlmutter-gpu-shared",
                hello_world=True,
                ntasks_per_node=8,
                gpus_per_node=4,
            )

        self.assertIn("--gpus-per-task=1 --gpu-bind=single:1 hostname", matched.script)
        self.assertNotIn("--gpu-bind", unmatched.script)

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
        # Completion would start a campaign build over SSH (tools/campaign.py).
        self.campaign_patch = patch.object(hr.campaign, "on_completion", return_value=None)
        self.campaign_patch.start()
        self.addCleanup(self.campaign_patch.stop)
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
            patch.object(hr, "INPUT_BACKUP_DIR", self.root / "input_backup"),
            patch.object(
                hr.notebook_csv, "NOTEBOOK_PATH", self.root / "simulation_notebook.csv"
            ),
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
            # The input is also appended to the archive under the directory
            # name, and the CSV notebook is rewritten from the catalog.
            backup = self.root / "input_backup" / self.simulation["directory_name"]
            self.assertTrue(result["input_backup"]["written"])
            self.assertIn("sml_mstep", backup.read_text(encoding="utf-8"))
            notebook = (self.root / "simulation_notebook.csv").read_text(encoding="utf-8")
            self.assertTrue(result["notebook"]["synced"])
            self.assertIn("n645st", notebook)

            finished = CommandResult(0, "COMPLETED 0:0", "", "squeue")
            with patch.object(hr.ssh, "run", return_value=finished):
                status = hr.status("stellar", "777")

            self.assertEqual(status["state"], "completed")
            self.assertTrue(status["catalog"]["updated"])
            self.assertEqual(hr.catalog.read_record("n645st")["status"], "completed")


if __name__ == "__main__":
    unittest.main()


class ReproductionProvenanceTests(unittest.TestCase):
    """What a rerun needs beyond the namelist: the files it names, the build."""

    def test_referenced_files_are_read_from_the_input_text(self) -> None:
        text = (
            "&sml_param\n"
            "sml_node_file='m.node'   ! mesh\n"
            "sml_input_file_dir='../XGC_inputs/'\n"
            "/\n&eq_param\n"
            "eq_filename='eq.eqd'\n"
            "eq_dens_file='den.dat' 'den.dat'\n"
            "/\n"
        )
        refs = hr.nlm.referenced_input_files(text)
        self.assertEqual(refs["input_file_dir"], "../XGC_inputs/")
        self.assertEqual(refs["files"]["eq_dens_file"], ["den.dat"])
        self.assertEqual(refs["files"]["sml_node_file"], ["m.node"])
        self.assertNotIn("sml_input_file_dir", refs["files"])

    def test_remote_output_is_parsed_into_hashes_and_build_settings(self) -> None:
        stdout = (
            "FILE\tm.node\tabc\t742543\t2026-07-21T19:32:40Z\n"
            "FILE\teq.eqd\tMISSING\n"
            "ENV\tdef\n"
            "CMAKE\tCMAKE_BUILD_TYPE:STRING=RelWithDebInfo\n"
            "CMAKE\tUSE_GPU:BOOL=ON\n"
            "CMAKE\tUNRELATED:BOOL=ON\n"
        )
        parsed = hr._parse_reproduction_output(stdout)
        self.assertEqual(parsed["files"]["m.node"]["sha256"], "abc")
        self.assertEqual(parsed["files"]["m.node"]["size_bytes"], 742543)
        self.assertEqual(parsed["missing"], ["eq.eqd"])
        self.assertEqual(parsed["env_sha256"], "def")
        self.assertEqual(
            parsed["cmake"], {"CMAKE_BUILD_TYPE": "RelWithDebInfo", "USE_GPU": "ON"}
        )

    def test_reproduction_provenance_runs_one_remote_command(self) -> None:
        cfg = {"paths": {"run_root": "/scratch"}, "xgc": {"build": "/scratch/XGC-Devel/bin"}}
        resources = {"executable": "/b/xgc", "env_script": "/b/env.sh", "xgc_commit": "abc123"}
        remote = CommandResult(0, "FILE\tm.node\tabc\t1\t2026-01-01T00:00:00Z\nENV\tdef\n", "", "bash")
        with (
            patch.object(hr.m, "get_machine", return_value=cfg),
            patch.object(hr.ssh, "host_for", return_value="perlmutter"),
            patch.object(hr.ssh, "run", return_value=remote) as run,
        ):
            result = hr._reproduction_provenance(
                "perlmutter", "/scratch/case", "&sml_param\nsml_node_file='m.node'\n/\n", resources
            )
        run.assert_called_once()
        script = run.call_args.args[1][2]
        self.assertIn("cd /scratch/case", script)
        self.assertIn("m.node", script)
        self.assertIn("/scratch/XGC-Devel/bin/CMakeCache.txt", script)
        files = result["referenced_inputs"]["files"]
        self.assertEqual(files["m.node"]["parameters"], ["sml_node_file"])
        self.assertEqual(files["m.node"]["sha256"], "abc")
        self.assertEqual(result["build_environment"]["env_script"]["sha256"], "def")
        self.assertEqual(result["build_environment"]["executable"], "/b/xgc")

    def test_ssh_failure_is_recorded_not_raised(self) -> None:
        cfg = {"paths": {"run_root": "/scratch"}}
        with (
            patch.object(hr.m, "get_machine", return_value=cfg),
            patch.object(hr.ssh, "host_for", return_value="perlmutter"),
            patch.object(hr.ssh, "run", side_effect=hr.ssh.SSHError("down")),
        ):
            result = hr._reproduction_provenance("perlmutter", "/c", "&sml_param\n/\n", {})
        self.assertIsNone(result["referenced_inputs"])
        self.assertEqual(result["error"], "down")

    def test_hello_world_has_nothing_to_hash(self) -> None:
        with patch.object(hr.ssh, "run") as run:
            result = hr._reproduction_provenance("perlmutter", None, None, {})
        run.assert_not_called()
        self.assertIsNone(result["referenced_inputs"])


class InputBackupTests(unittest.TestCase):
    """Every submitted input lands in input_backup/ under the directory name."""

    simulation = {"simulation_id": "n651po", "directory_name": "n651po_dadt_ecbc_df"}

    def test_first_submission_claims_the_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "INPUT_BACKUP_DIR", Path(tmp)):
                result = hr._backup_input(self.simulation, "&sml_param\n/\n")
            self.assertTrue(result["written"])
            self.assertEqual(
                (Path(tmp) / "n651po_dadt_ecbc_df").read_text(encoding="utf-8"),
                "&sml_param\n/\n",
            )

    def test_identical_resubmission_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "INPUT_BACKUP_DIR", Path(tmp)):
                hr._backup_input(self.simulation, "same")
                result = hr._backup_input(self.simulation, "same")
            self.assertFalse(result["written"])
            self.assertEqual(result["reason"], "identical")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_changed_input_becomes_a_dated_variant(self) -> None:
        from datetime import datetime, timezone

        when = datetime(2026, 9, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "INPUT_BACKUP_DIR", Path(tmp)):
                hr._backup_input(self.simulation, "first")
                second = hr._backup_input(self.simulation, "second", when=when)
                third = hr._backup_input(self.simulation, "third", when=when)
            self.assertTrue(second["written"])
            self.assertTrue(second["path"].endswith("n651po_dadt_ecbc_df.Sep10"))
            self.assertTrue(third["path"].endswith("n651po_dadt_ecbc_df.Sep10.bak2"))
            # The primary keeps the first input; variants never overwrite it.
            self.assertEqual((Path(tmp) / "n651po_dadt_ecbc_df").read_text(), "first")

    def test_variant_names_are_recognised_by_the_archive_scanner(self) -> None:
        from tools import input_archive

        self.assertEqual(input_archive._variant_kind("n651po_dadt_ecbc_df.Sep10") is not None, True)
        self.assertIsNone(input_archive._variant_kind("n651po_dadt_ecbc_df"))

    def test_unregistered_run_is_not_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hr, "INPUT_BACKUP_DIR", Path(tmp)):
                result = hr._backup_input(None, "text")
            self.assertFalse(result["written"])
            self.assertEqual(list(Path(tmp).iterdir()), [])


class ParallelDryrunTests(unittest.TestCase):
    """Machines without a login-node dryrun validate beside the queued job."""

    def setUp(self) -> None:
        self.cfg = {
            "scheduler": {
                "type": "pbs",
                "submit_tpl": "templates/polaris.pbs.j2",
                "system": "polaris",
                "filesystems": "home:grand",
            },
            "paths": {"run_root": "/grand/P/alice/xgc-runs"},
            "xgc": {
                "exe": "/grand/P/alice/XGC-Devel/bin/bin/xgc",
                "gpu_aware_mpi": False,
                "env_script": "/grand/P/alice/XGC-Devel/quickstart/modules_and_env/polaris.sh",
                "modules": [],
                "verified": {"commit": "abc123"},
            },
            "dryrun": {"login_node": False, "nodes": 1, "walltime": "00:15:00",
                       "start_grace_seconds": 0,
                       "queues": [{"name": "debug", "max_nodes": 2, "per_user": 1},
                                  {"name": "debug-scaling", "max_nodes": 10, "per_user": 1},
                                  {"name": "preemptable", "max_nodes": 10, "per_user": 20}]},
            "user": "alice",
            "defaults": {"account": "P", "queue": "prod", "nodes": 32, "ntasks_per_node": 4,
                         "cpus_per_task": 16, "gpus_per_node": 4, "walltime": "06:00:00"},
        }
        self.case = "/grand/P/alice/xgc-runs/n660po_case"
        self.submitted: list[str] = []
        self.written: dict[str, str] = {}
        # Other jobs the user already has, as (queue, state) -- the debug slot.
        self.user_jobs: list[tuple[str, str]] = []

    def _remote_factory(self, main_state: str):
        def _remote(host, argv, **kwargs):
            if argv[0] == "qsub":
                self.submitted.append(argv[-1])
                return CommandResult(0, f"{7700000 + len(self.submitted)}.polaris-pbs-01", "", "qsub")
            if argv[0] == "qselect":
                ids = [f"{900 + i}.polaris-pbs-01" for i in range(len(self.user_jobs))]
                return CommandResult(0, "\n".join(ids), "", "qselect")
            if argv[0] == "qstat" and "-F" in argv:
                jobs = {
                    f"{900 + i}.polaris-pbs-01": {"Job_Name": "other", "job_state": state, "queue": queue,
                                                   "Resource_List": {"nodect": 4}}
                    for i, (queue, state) in enumerate(self.user_jobs)
                }
                return CommandResult(0, json.dumps({"Jobs": jobs}), "", "qstat")
            if argv[0] == "qstat":
                return CommandResult(0, f"Job Id: x\n    job_state = {main_state}\n", "", "qstat")
            if argv[0] == "cat":
                return CommandResult(0, "&sml_param\nsml_mstep=10\n/\n", "", "cat")
            return CommandResult(0, "", "", " ".join(map(str, argv)))
        return _remote

    def _submit(self, main_state: str):
        def _write(host, path, text):
            self.written[path] = text
        with (
            patch.object(hr, "RUNS_DIR", Path(self.tmp.name) / "runs"),
            patch.object(hr.m, "get_machine", return_value=self.cfg),
            patch.object(hr.ssh, "host_for", return_value="polaris1"),
            patch.object(hr.ssh, "mkdir_p"),
            patch.object(hr.ssh, "write_file", side_effect=_write),
            patch.object(hr.ssh, "run", side_effect=self._remote_factory(main_state)),
            patch.object(hr.dryrun, "run_dryrun") as login_dryrun,
            patch.object(hr.time, "sleep") as sleep,
        ):
            result = hr.submit("polaris", confirm=True, case_dir=self.case, run_dir=self.case)
        login_dryrun.assert_not_called()
        return result, sleep

    def test_queued_job_gets_a_parallel_debug_dryrun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("Q")
            self.assertTrue(result["submitted"])
            self.assertIsNone(result["dryrun"])
            parallel = result["dryrun_parallel"]
            self.assertTrue(parallel["started"])
            self.assertEqual(parallel["main_job_state_after_grace"], "pending")
            self.assertEqual(parallel["job_id"], "7700002.polaris-pbs-01")
            self.assertEqual(parallel["n_ranks"], 128)
            self.assertEqual(parallel["queue"], "debug")
            self.assertEqual(parallel["stdout"], f"{self.case}/pbs-7700002.polaris-pbs-01.out")
            # Two scripts: the production one and the dryrun one, side by side.
            self.assertEqual(self.submitted, [f"{self.case}/submit.sh", parallel["script"]])
            self.assertTrue(parallel["script"].startswith(f"{self.case}/dryrun-"))
            script = self.written[parallel["script"]]
            self.assertIn("#PBS -q debug", script)
            self.assertIn("#PBS -l select=1:system=polaris", script)
            self.assertIn("#PBS -l walltime=00:15:00", script)
            self.assertIn('/grand/P/alice/XGC-Devel/bin/bin/xgc" --dryrun -n_ranks 128', script)
            self.assertIn("mpiexec --np 1 -ppn 1", script)
            provenance = json.loads(
                (Path(tmp) / "runs" / "test" / result["run_id"] / "provenance.json").read_text()
            )
            self.assertEqual(provenance["dryrun_parallel"]["job_id"], "7700002.polaris-pbs-01")
            self.assertIsNone(provenance["dryrun_parallel"]["report"])

    def test_a_taken_debug_slot_sends_the_dryrun_to_the_next_queue(self) -> None:
        self.user_jobs = [("debug", "R")]
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("Q")
            parallel = result["dryrun_parallel"]
            self.assertTrue(parallel["started"])
            self.assertEqual(parallel["queue"], "debug-scaling")
            self.assertIn("#PBS -q debug-scaling", self.written[parallel["script"]])
            self.assertIn("debug", parallel["queue_choice"]["skipped"])
            self.assertEqual(parallel["queue_choice"]["occupancy"], {"debug": 1})

    def test_queued_jobs_count_against_the_per_user_limit(self) -> None:
        # A queued job holds the slot as much as a running one does.
        self.user_jobs = [("debug", "Q"), ("debug-scaling", "Q")]
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("Q")
            parallel = result["dryrun_parallel"]
            self.assertTrue(parallel["started"])
            self.assertEqual(parallel["queue"], "preemptable")

    def test_finished_jobs_do_not_occupy_a_queue(self) -> None:
        self.user_jobs = [("debug", "F")]
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("Q")
            self.assertEqual(result["dryrun_parallel"]["queue"], "debug")

    def test_no_free_queue_leaves_the_run_unvalidated_and_says_so(self) -> None:
        self.cfg["dryrun"]["queues"] = [{"name": "debug", "max_nodes": 2, "per_user": 1}]
        self.user_jobs = [("debug", "R")]
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("Q")
            parallel = result["dryrun_parallel"]
            self.assertTrue(result["submitted"])
            self.assertFalse(parallel["started"])
            self.assertIn("no configured queue", parallel["error"])
            self.assertEqual(self.submitted, [f"{self.case}/submit.sh"])

    def test_choose_queue_respects_node_ranges(self) -> None:
        settings = hr.dryrun_settings({"dryrun": {"queues": [
            {"name": "debug-scaling", "min_nodes": 2, "max_nodes": 256, "per_user": 1},
            {"name": "capacity", "max_nodes": 16, "per_user": 5},
        ]}})
        choice = hr.choose_dryrun_queue(settings, {}, 1)
        self.assertEqual(choice["queue"], "capacity")
        self.assertIn("at least 2", choice["skipped"]["debug-scaling"])
        legacy = hr.dryrun_settings({"dryrun": {"queue": "debug"}})
        self.assertEqual(hr.choose_dryrun_queue(legacy, {}, 1)["queue"], "debug")

    def test_job_that_starts_at_once_needs_no_dryrun_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            result, _sleep = self._submit("R")
            parallel = result["dryrun_parallel"]
            self.assertFalse(parallel["started"])
            self.assertEqual(parallel["main_job_state_after_grace"], "running")
            self.assertEqual(self.submitted, [f"{self.case}/submit.sh"])

    def test_grace_period_is_honoured(self) -> None:
        self.cfg["dryrun"]["start_grace_seconds"] = 20
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = type("T", (), {"name": tmp})()
            _result, sleep = self._submit("R")
        sleep.assert_called_once_with(20.0)

    def test_login_node_machines_keep_the_blocking_dryrun(self) -> None:
        settings = hr.dryrun_settings({"paths": {}})
        self.assertTrue(settings["login_node"])
        self.assertEqual(settings["start_grace_seconds"], 20.0)

    def test_verdict_is_attached_when_the_dryrun_job_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(hr.catalog, "CATALOG_DIR", root / "simulations"), \
                 patch.object(hr, "RUNS_DIR", root / "runs"):
                hr.catalog.create_record("n660po", machines=["polaris"], short_name="case",
                                         directory_name="n660po_case")
                hr.catalog.attach_run("n660po", {
                    "run_id": "run-1", "machine": "polaris", "job_id": "7700001.polaris-pbs-01",
                    "remote_run_dir": self.case,
                    "dryrun_parallel": {"mode": "parallel", "started": True,
                                        "job_id": "7700002.polaris-pbs-01",
                                        "stdout": f"{self.case}/pbs-7700002.polaris-pbs-01.out",
                                        "stderr": f"{self.case}/pbs-7700002.polaris-pbs-01.err",
                                        "report": None},
                })

                def _remote(host, argv, **kwargs):
                    if argv[0] == "qstat":
                        return CommandResult(0, "Job Id: x\n    job_state = F\n    Exit_status = 0\n", "", "qstat")
                    if argv[0] == "cat" and argv[1].endswith(".out"):
                        return CommandResult(0, "Reading input\nDryrun completed\n", "", "cat")
                    return CommandResult(0, "", "", "cat")

                with patch.object(hr.m, "get_machine", return_value=self.cfg), \
                     patch.object(hr.ssh, "host_for", return_value="polaris1"), \
                     patch.object(hr.ssh, "run", side_effect=_remote):
                    first = hr.maybe_capture_parallel_dryrun("polaris", "n660po", "run-1")
                    second = hr.maybe_capture_parallel_dryrun("polaris", "n660po", "run-1")

                self.assertTrue(first["captured"])
                self.assertTrue(first["ok"])
                self.assertNotIn("warning", first)
                self.assertIsNone(second, "a captured verdict is not re-read")
                run = hr.catalog.read_record("n660po")["runs"][0]
                self.assertTrue(run["dryrun_parallel"]["report"]["completed"])
                self.assertEqual(run["dryrun_parallel"]["report"]["job_state"], "completed")

    def test_rejection_is_a_warning_not_an_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(hr.catalog, "CATALOG_DIR", root / "simulations"), \
                 patch.object(hr, "RUNS_DIR", root / "runs"):
                hr.catalog.create_record("n661po", machines=["polaris"], short_name="case",
                                         directory_name="n661po_case")
                hr.catalog.attach_run("n661po", {
                    "run_id": "run-1", "machine": "polaris", "job_id": "7700001.polaris-pbs-01",
                    "remote_run_dir": self.case,
                    "dryrun_parallel": {"mode": "parallel", "started": True,
                                        "job_id": "7700002.polaris-pbs-01",
                                        "stdout": f"{self.case}/pbs-7700002.polaris-pbs-01.out",
                                        "stderr": None, "report": None},
                })

                def _remote(host, argv, **kwargs):
                    if argv[0] == "qstat":
                        return CommandResult(0, "Job Id: x\n    job_state = F\n    Exit_status = 1\n", "", "qstat")
                    return CommandResult(0, "MPI_ABORT was invoked\n", "", "cat")

                with patch.object(hr.m, "get_machine", return_value=self.cfg), \
                     patch.object(hr.ssh, "host_for", return_value="polaris1"), \
                     patch.object(hr.ssh, "run", side_effect=_remote) as run:
                    verdict = hr.maybe_capture_parallel_dryrun("polaris", "n661po", "run-1")
                self.assertTrue(verdict["captured"])
                self.assertFalse(verdict["ok"])
                self.assertIn("REJECTED", verdict["warning"])
                self.assertIn("7700001", verdict["warning"])
                self.assertNotIn("qdel", [c.args[1][0] for c in run.call_args_list])

    def test_a_running_dryrun_job_is_retried_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(hr.catalog, "CATALOG_DIR", root / "simulations"), \
                 patch.object(hr, "RUNS_DIR", root / "runs"):
                hr.catalog.create_record("n662po", machines=["polaris"], short_name="case",
                                         directory_name="n662po_case")
                hr.catalog.attach_run("n662po", {
                    "run_id": "run-1", "machine": "polaris", "job_id": "1.x", "remote_run_dir": self.case,
                    "dryrun_parallel": {"started": True, "job_id": "2.x", "stdout": "o", "stderr": None, "report": None},
                })
                running = CommandResult(0, "Job Id: x\n    job_state = R\n", "", "qstat")
                with patch.object(hr.m, "get_machine", return_value=self.cfg), \
                     patch.object(hr.ssh, "host_for", return_value="polaris1"), \
                     patch.object(hr.ssh, "run", return_value=running):
                    verdict = hr.maybe_capture_parallel_dryrun("polaris", "n662po", "run-1")
                self.assertFalse(verdict["captured"])
                self.assertTrue(verdict["retry"])


class InputFileDirTests(unittest.TestCase):
    """sml_input_file_dir is character(len=65) in XGC, on every machine."""

    class _Plan:
        machine = "perlmutter"
        resources: dict = {}

        def to_dict(self) -> dict:
            return {}

    def _problem(self, value: str, *, require_absolute: bool = False) -> str | None:
        plan = self._Plan()
        plan.resources = {"daos": {"work_dir": "/tmp/pool/container/case"}}
        text = f"&sml_param\nsml_input_file_dir='{value}'\n/\n"
        with patch.object(hr.m, "get_machine", return_value={}), \
             patch.object(hr.ssh, "host_for", return_value="host"), \
             patch.object(hr.ssh, "run", return_value=CommandResult(0, text, "", "cat")):
            return hr._input_file_dir_problem(
                plan, "/case", require_absolute=require_absolute
            )

    def test_short_relative_path_is_fine_without_daos(self) -> None:
        self.assertIsNone(self._problem("../XGC_inputs/"))

    def test_relative_path_is_refused_under_daos(self) -> None:
        problem = self._problem("../XGC_inputs/", require_absolute=True)
        self.assertIsNotNone(problem)
        self.assertIn("relative", problem)

    def test_over_long_path_is_refused_on_any_machine(self) -> None:
        # The d3dqh dt=0 failure: 87 characters, truncated at 65, accepted by
        # xgc --dryrun and only caught once the job read the profiles.
        value = ("/pscratch/sd/s/sku/sangjun_turb_QH_newgrid_Frontier_emphase/"
                 "restart_07000_em/input_dir/")
        self.assertEqual(len(value), 87)
        problem = self._problem(value)
        self.assertIsNotNone(problem)
        self.assertIn("87 characters", problem)
        self.assertIn("65", problem)

    def test_exactly_at_the_limit_passes(self) -> None:
        self.assertIsNone(self._problem("/" + "a" * 63 + "/"))
        self.assertIsNotNone(self._problem("/" + "a" * 64 + "/"))


class DevDryrunDefaultTests(unittest.TestCase):
    """skip_dryrun=None skips the dryrun for dev (d-series) simulations only."""

    def test_dev_reservation_skips_by_default(self) -> None:
        self.assertTrue(hr._is_dev_simulation({"simulation_id": "d0009pe", "kind": "dev"}))
        # a reservation written before the kind field still counts by its prefix
        self.assertTrue(hr._is_dev_simulation({"simulation_id": "d0009pe"}))

    def test_production_and_unregistered_runs_keep_the_dryrun(self) -> None:
        self.assertFalse(hr._is_dev_simulation({"simulation_id": "n675pe", "kind": "production"}))
        self.assertFalse(hr._is_dev_simulation({"simulation_id": "n675pe"}))
        self.assertFalse(hr._is_dev_simulation(None))
