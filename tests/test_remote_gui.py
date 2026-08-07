from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import remote_gui as rg
from tools import ssh


MACHINE_CONFIG = {
    "host": "perlmutter.nersc.gov",
    "user": "alice",
    "auth": {"method": "sshproxy", "ssh_config_host": "perlmutter"},
    "gui": {
        "source": "/remote/XGC-Devel",
        "python": "/remote/venv/bin/python3",
        "env_script": "/remote/env.sh",
    },
}


class RemoteGUITargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self.tmp.name) / "runs"
        self.runs_dir.mkdir()
        self.runs_patch = patch.object(rg, "RUNS_DIR", self.runs_dir)
        self.runs_patch.start()

    def tearDown(self) -> None:
        self.runs_patch.stop()
        self.tmp.cleanup()

    def _write_run(self, run_id: str, **values: object) -> None:
        directory = self.runs_dir / run_id
        directory.mkdir()
        record = {
            "run_id": run_id,
            "machine": "perlmutter",
            "remote_run_dir": f"/pscratch/alice/{run_id}",
            **values,
        }
        (directory / "provenance.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    def test_plan_from_explicit_directory(self) -> None:
        with patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG):
            plan = rg.plan_remote_gui(
                "perlmutter",
                remote_directory="/pscratch/alice/case-a",
                local_port=51001,
                remote_port=41001,
                inspect_remote=False,
            )

        self.assertEqual(plan.target_source, "explicit")
        self.assertEqual(plan.remote_directory, "/pscratch/alice/case-a")
        self.assertEqual(plan.local_url, "http://127.0.0.1:51001/monitor")
        self.assertEqual(plan.authentication, "sshproxy")
        self.assertFalse(plan.control_master)
        self.assertFalse(plan.scheduler_used)

    def test_plan_resolves_run_provenance(self) -> None:
        self._write_run("run-a", simulation_id="n645pe", job_name="TCV test")
        with patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG):
            plan = rg.plan_remote_gui(
                "perlmutter",
                run_id="run-a",
                local_port=51001,
                remote_port=41001,
                inspect_remote=False,
            )

        self.assertEqual(plan.target_source, "run:run-a")
        self.assertEqual(plan.remote_directory, "/pscratch/alice/run-a")

    def test_query_requires_an_unambiguous_match(self) -> None:
        self._write_run("run-a", job_name="TCV comparison")
        self._write_run("run-b", job_name="TCV comparison")
        with (
            patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG),
            self.assertRaisesRegex(rg.RemoteGUIError, "ambiguous"),
        ):
            rg.plan_remote_gui(
                "perlmutter",
                query="TCV",
                inspect_remote=False,
            )

    def test_rejects_relative_and_root_directories(self) -> None:
        with patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG):
            for value in ("relative/case", "/"):
                with self.subTest(value=value):
                    with self.assertRaises(rg.RemoteGUIError):
                        rg.plan_remote_gui(
                            "perlmutter",
                            remote_directory=value,
                            inspect_remote=False,
                        )


class RemoteGUIInspectionTests(unittest.TestCase):
    def test_auto_preset_and_sources_are_discovered_read_only(self) -> None:
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        with (
            patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG),
            patch.object(rg.ssh, "host_for", return_value=target),
            patch.object(
                rg,
                "_inspect_remote",
                return_value={
                    "errors": [],
                    "presets": ["/case/view.xgcg"],
                    "sources": ["xgc.oneddiag.bp"],
                },
            ) as inspect,
        ):
            plan = rg.plan_remote_gui(
                "perlmutter",
                remote_directory="/case",
                preset="auto",
                local_port=51001,
                remote_port=41001,
            )

        self.assertEqual(plan.preset, "/case/view.xgcg")
        self.assertEqual(plan.discovered_sources, ("xgc.oneddiag.bp",))
        inspect.assert_called_once()

    def test_remote_inspection_parses_a_single_connection_result(self) -> None:
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        payload = {
            "errors": [],
            "presets": ["/case/view.xgcg"],
            "sources": ["xgc.oneddiag.bp"],
        }
        result = ssh.CommandResult(
            0,
            "XGC_REMOTE_GUI_JSON=" + json.dumps(payload, separators=(",", ":")),
            "",
            "python inspection",
        )
        with patch.object(rg.ssh, "run", return_value=result) as run:
            inspected = rg._inspect_remote(
                target,
                remote_directory="/case",
                app_path="/remote/XGC-Devel/utils/input_gui/app.py",
                gui_python="/remote/python3",
                env_script=None,
                monitor_source=None,
                preset="auto",
            )

        self.assertEqual(inspected, payload)
        run.assert_called_once()

    def test_remote_command_runs_from_target_with_monitor_startup(self) -> None:
        plan = rg.RemoteGUIPlan(
            machine="perlmutter",
            target_source="explicit",
            remote_directory="/case with space",
            monitor_source="xgc.oneddiag.bp",
            preset="/case with space/view.xgcg",
            gui_source="/remote/XGC-Devel",
            gui_python="/remote/venv/bin/python3",
            gui_env_script="/remote/env.sh",
            remote_port=41001,
            local_port=51001,
            local_url="http://127.0.0.1:51001/monitor",
            authentication="sshproxy",
            control_master=False,
            scheduler_used=False,
            discovered_sources=(),
        )

        command = rg._remote_command(plan)

        self.assertIn("cd '/case with space'", command)
        self.assertIn("XGC_INPUT_GUI_STARTUP_MODE=monitor", command)
        self.assertIn("XGC_INPUT_GUI_STARTUP_SOURCE=xgc.oneddiag.bp", command)
        self.assertIn("XGC_INPUT_GUI_HOST=127.0.0.1", command)
        self.assertIn("/remote/XGC-Devel/utils/input_gui/app.py", command)


class RemoteGUISessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.tmp.name) / "sessions"
        self.session_patch = patch.object(rg, "SESSION_DIR", self.session_dir)
        self.session_patch.start()
        self.plan = rg.RemoteGUIPlan(
            machine="perlmutter",
            target_source="explicit",
            remote_directory="/case",
            monitor_source=None,
            preset=None,
            gui_source="/remote/XGC-Devel",
            gui_python="/remote/python3",
            gui_env_script=None,
            remote_port=41001,
            local_port=51001,
            local_url="http://127.0.0.1:51001/monitor",
            authentication="sshproxy",
            control_master=False,
            scheduler_used=False,
            discovered_sources=(),
        )

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.tmp.cleanup()

    def test_launch_uses_one_independent_ssh_tunnel(self) -> None:
        process = unittest.mock.Mock(pid=12345)
        process.poll.return_value = None
        with (
            patch.object(rg.m, "get_machine", return_value=MACHINE_CONFIG),
            patch.object(
                rg.ssh,
                "host_for",
                return_value=ssh.SSHTarget("perlmutter", "sshproxy"),
            ),
            patch.object(
                rg.ssh,
                "connection_options",
                return_value=(
                    "perlmutter",
                    ["-o", "ControlMaster=no", "-o", "ControlPath=none"],
                ),
            ),
            patch.object(subprocess, "Popen", return_value=process) as popen,
            patch.object(rg, "_url_ready", return_value=True),
            patch.object(rg.webbrowser, "open") as browser,
        ):
            state = rg.launch_remote_gui(self.plan, open_browser=True)

        argv = popen.call_args.args[0]
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("127.0.0.1:51001:127.0.0.1:41001", argv)
        self.assertEqual(state["status"], "running")
        browser.assert_called_once_with(self.plan.local_url)

    def test_stop_does_not_signal_an_unowned_or_stale_pid(self) -> None:
        state = {
            "session_id": "gui-test",
            "status": "exited",
            "pid": 12345,
            "plan": self.plan.to_dict(),
        }
        rg._write_state(state)
        with (
            patch.object(rg, "_owned_process_alive", return_value=False),
            patch.object(rg.os, "killpg") as kill,
        ):
            result = rg.stop_remote_gui("gui-test")

        self.assertFalse(result["stopped"])
        kill.assert_not_called()

    def test_restricted_ps_falls_back_to_pid_and_loopback_url(self) -> None:
        state = {
            "session_id": "gui-test",
            "pid": 12345,
            "plan": self.plan.to_dict(),
        }
        with (
            patch.object(rg, "_process_command", return_value=None),
            patch.object(rg, "_pid_alive", return_value=True),
            patch.object(rg, "_url_ready", return_value=True),
        ):
            self.assertTrue(rg._owned_process_alive(state))


if __name__ == "__main__":
    unittest.main()
