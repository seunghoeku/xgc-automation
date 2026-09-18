from __future__ import annotations

import io
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from hpc_monitor import cli, config, install
from hpc_monitor.core import machines, queue_watch, ssh


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="monitor test ")
        self.root = Path(self.tmp.name)
        self.path = self.root / "servers.yaml"
        self.raw = {
            "version": 1, "notify": False,
            "servers": {"demo": {"host": "login.example.edu", "ssh_host": "demo-ssh",
                                  "user": "alice", "scheduler": "slurm", "auth": "key"}},
        }
        self.write()
        self.app = patch.object(config, "app_dir", return_value=self.root / "app")
        self.app.start()

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()

    def write(self):
        self.path.write_text(yaml.safe_dump(self.raw), encoding="utf-8")

    def test_config_needs_no_repository_and_restores_context(self):
        cfg = config.load(self.path)
        with patch.object(machines, "load_machines", side_effect=AssertionError("repository read")):
            with machines.using_machines(cfg.machines):
                self.assertEqual(machines.machine_names(), ["demo"])
                self.assertEqual(ssh.host_for(machines.get_machine("demo")).host, "demo-ssh")
                with machines.using_machines({"other": {}}):
                    self.assertEqual(machines.machine_names(), ["other"])
                self.assertEqual(machines.machine_names(), ["demo"])
            with self.assertRaises(AssertionError):
                machines.machine_names()

    def test_rejects_unsafe_names_and_config_typos(self):
        for field, value in [("ssh_host", "-oProxyCommand=bad"), ("user", "<username>"),
                             ("scheduler", "unknown"), ("auth", "password"),
                             ("enabled", "false"), ("typo", True)]:
            with self.subTest(field=field):
                original = self.raw["servers"]["demo"].copy()
                self.raw["servers"]["demo"][field] = value
                self.write()
                with self.assertRaises(config.ConfigError):
                    config.load(self.path)
                self.raw["servers"]["demo"] = original
        self.raw["servers"]["../escape"] = self.raw["servers"].pop("demo")
        self.write()
        with self.assertRaises(config.ConfigError):
            config.load(self.path)

    def test_duplicate_keys_are_not_silently_accepted(self):
        self.path.write_text("version: 1\nversion: 1\nservers: {}\n")
        with self.assertRaisesRegex(config.ConfigError, "Duplicate"):
            config.load(self.path)

    def test_cache_isolated_when_host_account_or_config_changes(self):
        before = config.load(self.path).state_files()["demo"]
        self.raw["servers"]["demo"]["user"] = "bob"
        self.write()
        after = config.load(self.path).state_files()["demo"]
        self.assertNotEqual(before, after)
        alternate = self.root / "other.yaml"
        alternate.write_bytes(self.path.read_bytes())
        self.assertNotEqual(after, config.load(alternate).state_files()["demo"])

    def test_menu_has_no_network_or_writes_and_respects_poll_interval(self):
        self.raw["poll_interval"] = 3600
        self.write()
        cfg = config.load(self.path)
        state = next(iter(cfg.state_files().values()))
        queue_watch.save_state(state, {
            "polled_at": (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(),
            "jobs": {"1": {"job_id": "1", "state": "running"}},
        })
        before = state.read_bytes()
        with patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                patch.object(queue_watch, "save_state", side_effect=AssertionError("write")):
            output = cli.render(cfg)
        self.assertIn("🟢 demo", output)
        self.assertNotIn("⚠", output)
        self.assertEqual(state.read_bytes(), before)

    def test_poll_tracks_only_observed_jobs_and_preserves_cache_on_disconnect(self):
        cfg = config.load(self.path)
        replies = [
            "42|my-run|RUNNING|0:01|1:00:00|2||/scratch/demo|N/A\n",
            "",
            "42|COMPLETED|0:0\n",
        ]
        def run(target, argv):
            self.assertEqual(target.host, "demo-ssh")
            self.assertTrue(any(cmd in argv for cmd in ("squeue", "sacct")))
            return ssh.CommandResult(0, replies.pop(0), "", "fixture")
        with patch.object(queue_watch, "probe", return_value={"pollable": True}), \
                patch.object(ssh, "run", side_effect=run):
            first = cli.poll(cfg)
            self.assertEqual(first["machines"][0]["recent_finished"], [])
            second = cli.poll(cfg)
        self.assertEqual(len(second["machines"][0]["recent_finished"]), 1)
        self.assertIn("✅ demo", cli.render(cfg))
        with patch.object(queue_watch, "probe", return_value={"pollable": False, "detail": "offline"}):
            cli.poll(cfg)
        self.assertIn("⚠ ✅ demo", cli.render(cfg))
        self.assertIn("my-run", cli.render(cfg))

    def test_poll_lock_prevents_overlapping_history_updates(self):
        cfg = config.load(self.path)
        with cli.poll_lock(cfg.cache_dir):
            with self.assertRaisesRegex(ValueError, "already running"):
                with cli.poll_lock(cfg.cache_dir):
                    pass
        with cli.poll_lock(cfg.cache_dir):
            pass

    def test_init_presets_include_public_hosts_but_no_personal_accounts(self):
        target = self.root / "new.yaml"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--config", str(target), "init"]), 0)
        data = yaml.safe_load(target.read_text())
        self.assertEqual(data["servers"]["perlmutter"]["host"], "perlmutter.nersc.gov")
        self.assertEqual(data["servers"]["aurora"]["scheduler"], "pbs")
        self.assertTrue(all(not v["enabled"] for v in data["servers"].values()))
        before = target.read_bytes()
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(cli.main(["--config", str(target), "init"]), 1)
        self.assertEqual(target.read_bytes(), before)

    def test_bad_config_keeps_a_visible_menu_item(self):
        self.path.write_text("invalid: true\n")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--config", str(self.path), "menu"]), 0)
        self.assertTrue(output.getvalue().startswith("HPC ⚠\n---\n"))

    def test_key_auth_never_requires_master_and_disables_interactive_auth(self):
        with patch.object(ssh, "control_socket_alive", side_effect=AssertionError("master")):
            _, options = ssh.connection_options(ssh.SSHTarget("demo", "key"))
        for expected in ("BatchMode=yes", "PreferredAuthentications=publickey",
                         "ControlPath=none", "ConnectTimeout=60"):
            self.assertIn(expected, options)

    def test_install_dry_run_install_update_and_uninstall(self):
        cfg = config.load(self.path)
        folder = self.root / "plugins with spaces"
        plist = self.root / "agents" / "monitor.plist"
        with patch.object(install, "app_dir", return_value=self.root / "app"), \
                patch.object(install, "service_file", return_value=plist), \
                patch.object(install, "log_file", return_value=self.root / "logs" / "poll.log"), \
                patch.object(install, "_launchctl") as ctl, \
                redirect_stdout(io.StringIO()):
            ctl.return_value.returncode = 1
            install.install(cfg, folder, dry_run=True, no_start=True)
            self.assertFalse(folder.exists())
            self.assertFalse(plist.exists())
            ctl.assert_not_called()
            install.install(cfg, folder, dry_run=False, no_start=True)
            data = plistlib.loads(plist.read_bytes())
            self.assertEqual(data["StartInterval"], 600)
            self.assertEqual(data["ProgramArguments"][4], str(cfg.path))
            self.assertEqual(data["ProgramArguments"][0], sys.executable)
            plugin = folder / install.PLUGIN
            # The generated shell script works even with spaces in config paths.
            result = subprocess.run(["/bin/bash", str(plugin)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("demo", result.stdout)
            install.install(cfg, folder, dry_run=False, no_start=True)
            install.uninstall(yes=False)
            self.assertTrue(plugin.exists())
            install.uninstall(yes=True)
            self.assertFalse(plist.exists())
            self.assertFalse(plugin.exists())
            self.assertTrue(self.path.exists())

    def test_install_refuses_to_overwrite_foreign_plugin(self):
        folder = self.root / "plugins"
        folder.mkdir()
        plugin = folder / install.PLUGIN
        plugin.write_text("#!/bin/bash\necho 'my plugin'\n")
        with self.assertRaisesRegex(ValueError, "unmanaged plugin"):
            install.install(config.load(self.path), folder, dry_run=False, no_start=True)


if __name__ == "__main__":
    unittest.main()
