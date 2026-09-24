"""Campaign archive automation (tools/campaign.py).

The remote scripts are executed for real by a local bash, against a fake
``python`` that stands in for XGC-Analysis's packager, so the shell that runs
on the login node is tested as shell rather than as a string.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import campaign
from tools import refresh as refresh_tool
from tools import simulation_catalog as sc
from tools import ssh

FAKE_PYTHON = r"""#!/bin/bash
# Stand-in for the remote interpreter. `-c` is the import check;
# `-m xgc_analysis.catalog.package_xgc_run` is the packager.
if [ "$1" = "-c" ]; then
  [ -f "$FAKE_STATE/no_import" ] && exit 1
  exit 0
fi
archive=
while [ $# -gt 0 ]; do
  case "$1" in
    --archive) archive=$2; shift ;;
  esac
  shift
done
echo "packaging into $archive"
rc=$(cat "$FAKE_STATE/rc" 2>/dev/null || echo 0)
if [ "$rc" = 0 ]; then printf 'sqlite' > "$archive"; else echo "hpc_campaign: boom" >&2; fi
exit "$rc"
"""

FAKE_TIMEOUT = """#!/bin/bash
shift
exec "$@"
"""


class CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.catalog_dir = root / "catalog"
        self.run_dir = root / "remote" / "n645pe_tcv"
        self.run_dir.mkdir(parents=True)
        self.state = root / "state"
        self.state.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("python", FAKE_PYTHON), ("timeout", FAKE_TIMEOUT)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = dict(os.environ, FAKE_STATE=str(self.state))
        self.env["PATH"] = f"{bin_dir}:{self.env['PATH']}"
        self.cfg: dict[str, Any] = {
            "campaign": {"auto": True, "python": str(bin_dir / "python"), "hostname": "TEST"},
        }
        self.ssh_calls: list[list[str]] = []
        self.unreachable = False

        patches = [
            patch.object(campaign.m, "get_machine", side_effect=lambda name: self.cfg),
            patch.object(campaign.ssh, "host_for", return_value="local"),
            patch.object(campaign.ssh, "run", side_effect=self._local_run),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _local_run(self, host: Any, argv: list[str], **kwargs: Any) -> ssh.CommandResult:
        self.ssh_calls.append(list(argv))
        if self.unreachable:
            raise ssh.SSHError("no live ssh session")
        proc = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=30)
        return ssh.CommandResult(proc.returncode, proc.stdout, proc.stderr, " ".join(argv))

    # -- helpers -----------------------------------------------------------

    def make(self, record_id: str = "n645pe", runs: tuple[str, ...] = ("run-a",)) -> None:
        sc.create_record(
            record_id, catalog_dir=self.catalog_dir, machines=["perlmutter"], short_name="tcv"
        )
        for index, run_id in enumerate(runs):
            sc.attach_run(
                record_id,
                {
                    "run_id": run_id,
                    "machine": "perlmutter",
                    "job_id": str(100 + index),
                    "submitted_at": f"2026-09-0{index + 1}T00:00:00Z",
                    "remote_run_dir": str(self.run_dir),
                },
                catalog_dir=self.catalog_dir,
            )

    def complete(self, run_id: str, record_id: str = "n645pe") -> None:
        sc.update_run_state(record_id, run_id, state="completed", exit_code=0, catalog_dir=self.catalog_dir)

    def wait_for_result(self) -> None:
        result = self.run_dir / campaign.WORK_DIRNAME / "result.json"
        deadline = time.time() + 15
        while not result.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(result.exists(), "the detached packager never wrote result.json")

    def stored(self, record_id: str = "n645pe") -> dict[str, Any]:
        return sc.read_record(record_id, catalog_dir=self.catalog_dir).get("campaign") or {}

    # -- tests ---------------------------------------------------------------

    def test_completion_builds_the_archive_and_status_collects_it(self) -> None:
        self.make()
        self.complete("run-a")

        started = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.assertTrue(started["started"])
        self.assertEqual(self.stored()["state"], "building")

        self.wait_for_result()
        final = campaign.status("n645pe", catalog_dir=self.catalog_dir)

        archive = self.run_dir / "n645pe.aca"
        self.assertEqual(final["state"], "built")
        self.assertEqual(final["for_run_id"], "run-a")
        self.assertEqual(final["size_bytes"], len("sqlite"))
        self.assertTrue(archive.is_file())
        self.assertFalse((self.run_dir / campaign.WORK_DIRNAME / "n645pe.aca").exists())
        self.assertFalse((self.run_dir / campaign.WORK_DIRNAME / "lock").exists())
        # Built for this run: a repeated completion signal does nothing.
        calls = len(self.ssh_calls)
        self.assertIsNone(campaign.on_completion("n645pe", catalog_dir=self.catalog_dir))
        self.assertEqual(len(self.ssh_calls), calls)

    def test_packager_failure_leaves_no_archive(self) -> None:
        (self.state / "rc").write_text("3")
        self.make()
        self.complete("run-a")
        campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.wait_for_result()

        final = campaign.status("n645pe", catalog_dir=self.catalog_dir)

        self.assertEqual(final["state"], "failed")
        self.assertIn("exited 3", final["reason"])
        self.assertIn("boom", final["reason"])
        self.assertFalse((self.run_dir / "n645pe.aca").exists())
        # Failures are retried by hand, not by every refresh.
        self.assertEqual(campaign.follow_up(catalog_dir=self.catalog_dir), [])

    def test_missing_environment_stays_pending_until_it_is_installed(self) -> None:
        (self.state / "no_import").write_text("")
        self.make()
        self.complete("run-a")

        first = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.assertEqual(first["state"], "pending")
        self.assertIn("cannot import", first["reason"])

        (self.state / "no_import").unlink()
        retried = campaign.follow_up(catalog_dir=self.catalog_dir)
        self.assertEqual(retried[0]["state"], "building")
        self.wait_for_result()
        self.assertEqual(campaign.follow_up(catalog_dir=self.catalog_dir)[0]["state"], "built")

    def test_unreachable_machine_is_pending(self) -> None:
        self.make()
        self.complete("run-a")
        self.unreachable = True
        result = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.assertEqual(result["state"], "pending")
        self.assertIn("ssh", result["reason"])

    def test_an_archive_it_did_not_make_is_never_touched(self) -> None:
        archive = self.run_dir / "n645pe.aca"
        archive.write_text("human-made")
        self.make()
        self.complete("run-a")

        result = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)

        self.assertEqual(result["state"], "failed")
        self.assertIn("already exists", result["reason"])
        self.assertEqual(archive.read_text(), "human-made")

    def test_later_completion_marks_the_archive_stale_and_replace_needs_confirm(self) -> None:
        self.make(runs=("run-a",))
        self.complete("run-a")
        campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.wait_for_result()
        campaign.status("n645pe", catalog_dir=self.catalog_dir)
        archive = self.run_dir / "n645pe.aca"
        archive.write_text("from run-a")

        # A restart completes after the archive was made.
        sc.attach_run(
            "n645pe",
            {
                "run_id": "run-b",
                "machine": "perlmutter",
                "job_id": "200",
                "submitted_at": "2026-09-05T00:00:00Z",
                "remote_run_dir": str(self.run_dir),
            },
            catalog_dir=self.catalog_dir,
        )
        self.complete("run-b")
        calls = len(self.ssh_calls)
        stale = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["for_run_id"], "run-a")
        self.assertEqual(len(self.ssh_calls), calls, "a stale archive must not trigger a rebuild")
        self.assertEqual(archive.read_text(), "from run-a")

        plan = campaign.build("n645pe", replace=True, catalog_dir=self.catalog_dir)
        self.assertTrue(plan["confirm_required"])
        self.assertEqual(archive.read_text(), "from run-a")

        rebuilt = campaign.build("n645pe", replace=True, confirm=True, catalog_dir=self.catalog_dir)
        self.assertTrue(rebuilt["started"])
        self.wait_for_result()
        final = campaign.status("n645pe", catalog_dir=self.catalog_dir)
        self.assertEqual(final["state"], "built")
        self.assertEqual(final["for_run_id"], "run-b")
        self.assertNotIn("stale", final)
        self.assertEqual(archive.read_text(), "sqlite")

    def test_auto_off_or_dev_kind_does_nothing(self) -> None:
        self.make()
        self.complete("run-a")
        self.cfg["campaign"]["auto"] = False
        self.assertIsNone(campaign.on_completion("n645pe", catalog_dir=self.catalog_dir))

        self.cfg["campaign"]["auto"] = True
        self.make("d0001pe")
        self.complete("run-a", record_id="d0001pe")
        self.assertIsNone(campaign.on_completion("d0001pe", catalog_dir=self.catalog_dir))
        self.assertEqual(self.ssh_calls, [])

    def test_not_completed_does_nothing(self) -> None:
        self.make()
        sc.update_run_state("n645pe", "run-a", state="timeout", catalog_dir=self.catalog_dir)
        self.assertIsNone(campaign.on_completion("n645pe", catalog_dir=self.catalog_dir))

    def test_missing_hostname_keeps_the_build_pending(self) -> None:
        del self.cfg["campaign"]["hostname"]
        self.make()
        self.complete("run-a")
        result = campaign.on_completion("n645pe", catalog_dir=self.catalog_dir)
        self.assertEqual(result["state"], "pending")
        self.assertIn("campaign.hostname", result["reason"])
        self.assertEqual(self.ssh_calls, [])

    def test_manual_build_without_confirm_only_plans(self) -> None:
        self.cfg["campaign"]["hostname"] = "NERSC"
        self.make()
        self.complete("run-a")

        plan = campaign.build("n645pe", catalog_dir=self.catalog_dir)

        self.assertTrue(plan["confirm_required"])
        self.assertIn("xgc_analysis.catalog.package_xgc_run", plan["command"])
        self.assertIn("--hostname NERSC", plan["command"])
        self.assertIn(f"--archive {self.run_dir}/{campaign.WORK_DIRNAME}/n645pe.aca", plan["command"])
        self.assertEqual(self.ssh_calls, [])


class SettingsTests(unittest.TestCase):
    def test_falls_back_to_the_analysis_runtime_and_clamps_the_timeout(self) -> None:
        resolved = campaign.settings(
            {
                "analysis": {"python": "/env/bin/python", "pythonpath": ["/a", "/b"]},
                "xgc": {"env_script": "/xgc/env.sh"},
                "campaign": {"timeout_seconds": 10 * 3600},
            }
        )
        self.assertFalse(resolved["auto"])
        self.assertEqual(resolved["python"], "/env/bin/python")
        self.assertEqual(resolved["pythonpath"], "/a:/b")
        self.assertEqual(resolved["env_script"], "/xgc/env.sh")
        self.assertEqual(resolved["timeout_seconds"], campaign.LOGIN_NODE_MAX_SECONDS)
        self.assertEqual(resolved["auto_kinds"], ["production"])


class RefreshHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_refresh_starts_a_campaign_only_on_the_transition_to_completed(self) -> None:
        sc.create_record("n645pe", catalog_dir=self.dir, machines=["perlmutter"], short_name="tcv")
        sc.attach_run(
            "n645pe",
            {"run_id": "run-a", "machine": "perlmutter", "job_id": "1"},
            catalog_dir=self.dir,
        )

        def status_fn(machine: str, job_id: str) -> dict[str, Any]:
            return {"state": "completed", "exit_code": 0, "terminal": True}

        with patch.object(refresh_tool.campaign, "on_completion", return_value={"record_id": "n645pe", "state": "building"}) as hook, \
             patch.object(refresh_tool.campaign, "follow_up", return_value=[]) as follow, \
             patch.object(refresh_tool.hpc_runner, "maybe_capture_build", return_value=None), \
             patch.object(refresh_tool.hpc_runner, "maybe_capture_parallel_dryrun", return_value=None):
            result = refresh_tool.refresh(catalog_dir=self.dir, status_fn=status_fn, campaign_hooks=True)
            again = refresh_tool.refresh(catalog_dir=self.dir, status_fn=status_fn, campaign_hooks=True)

        hook.assert_called_once_with("n645pe", catalog_dir=self.dir)
        self.assertEqual(result["campaigns"], [{"record_id": "n645pe", "state": "building"}])
        self.assertEqual(again["campaigns"], [])
        self.assertEqual(follow.call_args_list[0].kwargs["skip"], {"n645pe"})

    def test_hooks_are_off_when_a_status_fn_is_injected(self) -> None:
        with patch.object(refresh_tool.campaign, "follow_up") as follow:
            result = refresh_tool.refresh(catalog_dir=self.dir, status_fn=lambda *_: {})
        follow.assert_not_called()
        self.assertEqual(result["campaigns"], [])


if __name__ == "__main__":
    unittest.main()
