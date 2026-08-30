from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import ssh


class CommandChannelTests(unittest.TestCase):
    def test_reports_usable_noninteractive_channel(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="", stderr=""
        )
        with patch.object(subprocess, "run", return_value=completed) as run:
            usable, detail = ssh.command_channel_usable("perlmutter")

        self.assertTrue(usable)
        self.assertEqual(detail, "command channel usable")
        self.assertIn("BatchMode=yes", run.call_args.args[0])

    def test_reports_master_that_cannot_open_another_channel(self) -> None:
        with patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10.0),
        ):
            usable, detail = ssh.command_channel_usable("frontier")

        self.assertFalse(usable)
        self.assertIn("timed out", detail)


class CertificateStatusTests(unittest.TestCase):
    def test_reads_certificate_selected_by_ssh_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            certificate = f"{tmp}/nersc-cert.pub"
            with open(certificate, "w", encoding="utf-8") as stream:
                stream.write("fixture")
            effective = subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout=f"hostname perlmutter.nersc.gov\ncertificatefile {certificate}\n",
                stderr="",
            )
            decoded = subprocess.CompletedProcess(
                args=["ssh-keygen"],
                returncode=0,
                stdout=(
                    "Valid: from 2000-01-01T00:00:00 "
                    "to 2999-01-01T00:00:00\n"
                ),
                stderr="",
            )
            with patch.object(subprocess, "run", side_effect=[effective, decoded]):
                status = ssh.certificate_status("perlmutter")

        self.assertIsNotNone(status)
        self.assertTrue(status["present"])
        self.assertTrue(status["valid"])
        self.assertEqual(status["path"], certificate)


class DirectKeyTransportTests(unittest.TestCase):
    def test_sshproxy_run_forces_direct_public_key_connection(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="ok\n", stderr=""
        )
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        certificate = {"present": True, "valid": True, "detail": "current"}

        with (
            patch.object(ssh, "certificate_status", return_value=certificate),
            patch.object(ssh, "control_socket_alive") as control,
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            result = ssh.run(target, ["hostname"], check=True)

        self.assertEqual(result.stdout, "ok\n")
        # A direct-key host never consults the user's ControlMaster socket.
        control.assert_not_called()
        argv = run.call_args.args[0]
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("PreferredAuthentications=publickey", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=60", argv)
        self.assertEqual(argv[-2:], ["perlmutter", "hostname"])

    def test_direct_key_connections_reuse_a_tool_owned_socket(self) -> None:
        # A direct-key handshake to NERSC costs seconds, and bulk work makes
        # many calls in a row, so the connection is shared.
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="ok\n", stderr=""
        )
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        certificate = {"present": True, "valid": True, "detail": "current"}

        with (
            patch.object(ssh, "certificate_status", return_value=certificate),
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            ssh.run(target, ["hostname"])

        argv = run.call_args.args[0]
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("ControlPersist=10m", argv)
        socket = str(ssh.control_socket_path("perlmutter"))
        self.assertIn(f"ControlPath={socket}", argv)

    def test_the_shared_socket_is_not_the_users_own(self) -> None:
        # Sharing the user's socket would couple automation to the lifetime of
        # their tmux client or VS Code Remote session, which is what
        # disabling multiplexing was originally protecting against.
        socket = ssh.control_socket_path("perlmutter")
        self.assertEqual(socket.parent, ssh.CONTROL_DIR)
        self.assertNotIn("cm-", socket.name)

    def test_socket_path_stays_within_the_unix_limit(self) -> None:
        # macOS rejects unix socket paths past ~104 characters, and ssh fails
        # obscurely when it happens.
        for host in ("perlmutter", "a" * 200, "user@host.very.long.example.org"):
            with self.subTest(host=host):
                self.assertLess(len(str(ssh.control_socket_path(host))), 104)

    def test_distinct_hosts_get_distinct_sockets(self) -> None:
        self.assertNotEqual(
            ssh.control_socket_path("perlmutter"), ssh.control_socket_path("frontier")
        )

    def test_multiplexing_can_be_turned_off(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout="ok\n", stderr=""
        )
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        certificate = {"present": True, "valid": True, "detail": "current"}

        with (
            patch.dict(os.environ, {"XGC_SSH_NO_MULTIPLEX": "1"}),
            patch.object(ssh, "certificate_status", return_value=certificate),
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            ssh.run(target, ["hostname"])

        argv = run.call_args.args[0]
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("ControlPath=none", argv)

    def test_sshproxy_rejects_expired_certificate_before_connecting(self) -> None:
        target = ssh.SSHTarget("perlmutter", "sshproxy")
        certificate = {"present": True, "valid": False, "detail": "expired"}

        with (
            patch.object(ssh, "certificate_status", return_value=certificate),
            patch.object(subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ssh.SSHError, "missing or expired"):
                ssh.run(target, ["true"])

        run.assert_not_called()

    def test_controlmaster_auth_still_requires_a_live_session(self) -> None:
        target = ssh.SSHTarget("polaris4", "ssh_tunnel")
        with patch.object(
            ssh, "control_socket_alive", return_value=(False, "no master")
        ):
            with self.assertRaisesRegex(ssh.SSHError, "no live ssh session"):
                ssh.run(target, ["true"])

    def test_host_for_carries_the_authentication_method(self) -> None:
        target = ssh.host_for(
            {
                "host": "perlmutter.nersc.gov",
                "user": "alice",
                "auth": {
                    "method": "sshproxy",
                    "ssh_config_host": "perlmutter",
                },
            }
        )

        self.assertEqual(target, ssh.SSHTarget("perlmutter", "sshproxy"))


if __name__ == "__main__":
    unittest.main()
