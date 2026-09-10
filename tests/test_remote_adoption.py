from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import remote_adoption as ra
from tools import simulation_catalog as sc
from tools.ssh import CommandResult

QSTAT = """Job Id: 7597264.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov
    Job_Name = n648po
    job_state = F
    queue = medium
    qtime = Mon Sep  7 13:17:08 2026
    stime = Mon Sep  7 13:17:55 2026
    obittime = Mon Sep  7 19:19:16 2026
    Resource_List.nodect = 32
    Resource_List.walltime = 06:00:00
    resources_used.walltime = 06:01:21
    comment = Job run at Mon Sep 07 at 13:17 on (x3008c0s25b1n0:ncpus=64:ngpus=4)+(x30
\t08c0s25b0n0:ncpus=64:ngpus=4)
    Exit_status = -29
"""

BANNER = """host=x3008c0s25b1n0 job=7597264
--------------------------------------------------------------
MPI_COMM_WORLD communicator : 32 nodes, 128 MPI tasks
--------------------------------------------------------------
BUILD DATE:    Sat Sep  5 01:08:15 PM UTC 2026
HOST:          polaris-login-01
BUILT BY:      shku
CXX COMPILER:  GNU 14.3.0
BUILD TYPE:    RelWithDebInfo
XGC_FLAGS:
GIT INFO:
    origin	git@github.com:seunghoeku/XGC-Devel.git (fetch)
    4cce91da1f19c9a5d3ea41fb99449154a8c0131a Fix push_As
    Working tree is clean.
"""

INPUT = "&sml_param\nsml_node_file='ma.node'\nsml_input_file_dir='../XGC_inputs/'\n/\n&eq_param\neq_filename='ma.eqd'\n/\n"
SCRIPT = "#!/bin/bash -l\n#PBS -N n648po\nSRC=/grand/shku/XGC-Devel\nXGC_BIN=${SRC}/bin/bin/xgc\nsource \"${SRC}/quickstart/modules_and_env/polaris.sh\"\nmpiexec ${XGC_BIN}\n"
REMOTE = "/grand/shku/n648po_dAdt_ecbc"


def fake_ssh(host, argv, **kwargs):
    if argv[0] == "bash":
        script = argv[2]
        if "### SCRIPTS" in script:
            return CommandResult(0, (
                f"### SCRIPTS\nxgcs.pbs\ndryrun.pbs\n### STDOUT\nn648po.o7597264\nxgc.out.7597264\n"
                "failed_7597263/xgc.out.7597263\n### TIMESTEP\n2000\n### LAST3D\nxgc.3d.02040.bp\n### RESTART\n"
                "xgc.restart.02000.bp\n### END\n"), "", "bash")
        # the reproduction script
        return CommandResult(0, "FILE\tma.node\tabc\t10\t2026-01-01T00:00:00Z\nFILE\tma.eqd\tMISSING\nENV\tdef\n", "", "bash")
    if argv[0] == "qstat":
        if argv[-1] == "7597263":
            return CommandResult(0, QSTAT.replace("7597264", "7597263").replace("Exit_status = -29", "Exit_status = 143").replace("06:01:21", "00:00:29"), "", "qstat")
        return CommandResult(0, QSTAT, "", "qstat")
    if argv[0] == "head":
        if argv[-1].endswith("xgc.out.7597264"):
            return CommandResult(0, BANNER, "", "head")
        return CommandResult(0, "no banner here\n", "", "head")
    if argv[0] == "cat":
        if argv[-1].endswith("xgcs.pbs"):
            return CommandResult(0, SCRIPT, "", "cat")
        if argv[-1].endswith("dryrun.pbs"):
            return CommandResult(0, "#!/bin/bash\n#PBS -N dry\n", "", "cat")
        if argv[-1].endswith("/input"):
            return CommandResult(0, INPUT, "", "cat")
    return CommandResult(1, "", f"unexpected {argv}", " ".join(argv))


CFG = {
    "scheduler": {"type": "pbs"},
    "paths": {"run_root": "/grand/shku"},
    "xgc": {"exe": "/cfg/xgc", "env_script": "/cfg/env.sh", "build": "/cfg/build", "verified": {"commit": "cfgcommit"}},
}


class ParsingTests(unittest.TestCase):
    def test_job_ids_come_from_stdout_names_with_xgc_out_first(self) -> None:
        jobs = ra.job_ids_from_stdout_names(
            ["n648po.o7597264", "xgc.out.7597264", "slurm-123.out", "failed_1/xgc.out.7597263", "notes.txt"]
        )
        self.assertEqual(jobs["7597264"], ["xgc.out.7597264", "n648po.o7597264"])
        self.assertEqual(jobs["123"], ["slurm-123.out"])
        self.assertEqual(jobs["7597263"], ["failed_1/xgc.out.7597263"])

    def test_qstat_full_is_flattened_including_continuation_lines(self) -> None:
        fields = ra.parse_qstat_full(QSTAT)
        self.assertEqual(fields["Exit_status"], "-29")
        self.assertEqual(fields["Resource_List.nodect"], "32")
        self.assertIn("x3008c0s25b0n0", fields["comment"])

    def test_pbs_states_follow_the_exit_status(self) -> None:
        base = ra.parse_qstat_full(QSTAT)
        suffix = ".polaris-pbs-01"
        self.assertEqual(ra.pbs_run_from_fields(base, "1", suffix)["state"], "timeout")
        self.assertEqual(ra.pbs_run_from_fields({**base, "Exit_status": "0"}, "1", suffix)["state"], "completed")
        self.assertEqual(ra.pbs_run_from_fields({**base, "Exit_status": "143"}, "1", suffix)["state"], "cancelled")
        self.assertEqual(ra.pbs_run_from_fields({**base, "Exit_status": "1"}, "1", suffix)["state"], "failed")
        live = ra.pbs_run_from_fields({**base, "job_state": "R"}, "1", suffix)
        self.assertEqual(live["state"], "running")
        never_ran = ra.pbs_run_from_fields(
            {"job_state": "F", "comment": "Not Running: Not enough free nodes available and terminated"}, "2", suffix
        )
        self.assertEqual(never_ran["state"], "cancelled")
        entry = ra.pbs_run_from_fields(base, "7597264", suffix)
        self.assertEqual(entry["job_id"], "7597264.polaris-pbs-01")
        self.assertEqual(entry["submitted_at"], "2026-09-07T13:17:08")
        self.assertEqual(entry["nodes"], 32)
        self.assertEqual(entry["exit_code"], -29)

    def test_sacct_row_of_the_job_itself_is_used(self) -> None:
        stdout = (
            "123|n650pe|TIMEOUT|0:0|32|2026-09-01T10:00:00|2026-09-01T10:05:00|2026-09-01T16:05:00|06:00:00|06:00:00|regular\n"
            "123.batch|batch|CANCELLED|0:15|32|...|...|...|06:00:00||\n"
        )
        entry = ra.slurm_run_from_sacct(stdout, "123")
        self.assertEqual(entry["state"], "timeout")
        self.assertEqual(entry["nodes"], 32)
        self.assertEqual(entry["run_id"], "slurm-123")
        self.assertIsNone(ra.slurm_run_from_sacct(stdout, "999"))

    def test_script_resources_expand_src_and_fall_back_to_config(self) -> None:
        resources = ra._script_resources(SCRIPT, CFG)
        self.assertEqual(resources["executable"], "/grand/shku/XGC-Devel/bin/bin/xgc")
        self.assertEqual(resources["env_script"], "/grand/shku/XGC-Devel/quickstart/modules_and_env/polaris.sh")
        self.assertEqual(resources["build_dir"], "/grand/shku/XGC-Devel/bin")
        self.assertEqual(resources["xgc_commit"], "cfgcommit")
        fallback = ra._script_resources("", CFG)
        self.assertEqual(fallback["executable"], "/cfg/xgc")
        self.assertEqual(fallback["resources_source"], "machines config")


class AdoptRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.catalog_dir = self.root / "simulations"
        self.backup_dir = self.root / "input_backup"
        self.adopted_dir = self.root / "adopted"
        self.patches = [
            patch.object(ra.m, "get_machine", return_value=CFG),
            patch.object(ra.hpc_runner.m, "get_machine", return_value=CFG),
            patch.object(ra.ssh, "host_for", return_value="polaris1"),
            patch.object(ra.ssh, "run", side_effect=fake_ssh),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def test_plan_reads_everything_and_writes_nothing(self) -> None:
        payload = ra.adopt_remote("polaris", REMOTE, catalog_dir=self.catalog_dir,
                                  input_backup_dir=self.backup_dir, adopted_dir=self.adopted_dir)
        self.assertFalse(payload["adopted"])
        record = payload["record"]
        self.assertEqual(record["record_id"], "n648po_dadt_ecbc")
        self.assertEqual(record["origin"], "adopted")
        self.assertIsNone(record["number"])
        self.assertEqual(record["adoption"]["number_in_name"], 648)
        self.assertEqual(record["simulation_tag"], "n648po")
        self.assertEqual([r["state"] for r in record["runs"]], ["cancelled", "timeout"])
        # timeout on the newest run -> interrupted, exactly as for tool-driven runs
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["runs"][1]["build"]["git"]["commit"], "4cce91da1f19c9a5d3ea41fb99449154a8c0131a")
        self.assertEqual(record["xgc_commit"], "4cce91da1f19c9a5d3ea41fb99449154a8c0131a")
        self.assertEqual(record["referenced_inputs"]["files"]["ma.node"]["sha256"], "abc")
        self.assertEqual(record["referenced_inputs"]["missing"], ["ma.eqd"])
        self.assertEqual(record["build_environment"]["executable"], "/grand/shku/XGC-Devel/bin/bin/xgc")
        self.assertEqual(record["progress"]["last_gstep"], 2040)
        self.assertEqual(record["input"]["parameters"]["sml_node_file"], "ma.node")
        self.assertNotIn("input_text", payload["inspection"])
        self.assertFalse(self.catalog_dir.exists())
        self.assertFalse(self.backup_dir.exists())

    def test_confirm_writes_record_backup_and_support_files(self) -> None:
        payload = ra.adopt_remote("polaris", REMOTE, confirm=True, tags=["dadt"], case="dadt-ecbc-em",
                                  note="hand run", catalog_dir=self.catalog_dir,
                                  input_backup_dir=self.backup_dir, adopted_dir=self.adopted_dir)
        self.assertTrue(payload["adopted"])
        record = sc.read_record("n648po_dadt_ecbc", catalog_dir=self.catalog_dir)
        self.assertEqual(record["tags"], ["adopted", "dadt"])
        self.assertEqual(record["case"], "dadt-ecbc-em")
        self.assertEqual(record["note"], "hand run")
        self.assertEqual((self.backup_dir / "n648po_dAdt_ecbc").read_text(), INPUT)
        self.assertTrue(record["input_backup"]["written"])
        support = self.adopted_dir / "n648po_dadt_ecbc"
        self.assertEqual((support / "xgcs.pbs").read_text(), SCRIPT)
        self.assertTrue((support / "banner_7597264.json").exists())
        self.assertEqual(record["job_script"]["local_copy"].split("/")[-1], "xgcs.pbs")
        # searchable like any other record
        hits = sc.search("dadt", catalog_dir=self.catalog_dir)
        self.assertEqual([h["record_id"] for h in hits], ["n648po_dadt_ecbc"])

    def test_second_adoption_needs_overwrite_and_keeps_the_note(self) -> None:
        kwargs = dict(catalog_dir=self.catalog_dir, input_backup_dir=self.backup_dir, adopted_dir=self.adopted_dir)
        ra.adopt_remote("polaris", REMOTE, confirm=True, note="keep me", **kwargs)
        with self.assertRaisesRegex(ra.RemoteAdoptionError, "overwrite"):
            ra.adopt_remote("polaris", REMOTE, confirm=True, **kwargs)
        ra.adopt_remote("polaris", REMOTE, confirm=True, overwrite=True, **kwargs)
        record = sc.read_record("n648po_dadt_ecbc", catalog_dir=self.catalog_dir)
        self.assertEqual(record["note"], "keep me")
        self.assertEqual(record["input_backup"]["reason"], "identical")

    def test_a_registered_simulation_is_never_overwritten(self) -> None:
        sc.create_record("n648po_dadt_ecbc", catalog_dir=self.catalog_dir, machines=["polaris"],
                         short_name="x", directory_name="n648po_dAdt_ecbc", origin="automation")
        payload = ra.adopt_remote("polaris", REMOTE, catalog_dir=self.catalog_dir,
                                  input_backup_dir=self.backup_dir, adopted_dir=self.adopted_dir)
        self.assertIn("never overwritten", payload["blocked"])
        with self.assertRaisesRegex(ra.RemoteAdoptionError, "never overwritten"):
            ra.adopt_remote("polaris", REMOTE, confirm=True, catalog_dir=self.catalog_dir,
                            input_backup_dir=self.backup_dir, adopted_dir=self.adopted_dir)

    def test_missing_directory_is_an_error(self) -> None:
        def missing(host, argv, **kwargs):
            if argv[0] == "bash":
                return CommandResult(0, "### MISSING\n", "", "bash")
            return CommandResult(1, "", "", "")
        with patch.object(ra.ssh, "run", side_effect=missing):
            with self.assertRaisesRegex(ra.RemoteAdoptionError, "not readable"):
                ra.inspect_remote_run("polaris", "/grand/nope")


if __name__ == "__main__":
    unittest.main()
