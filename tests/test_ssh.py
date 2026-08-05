from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
