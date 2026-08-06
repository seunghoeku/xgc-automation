from __future__ import annotations

import unittest
from unittest.mock import patch

from tools import server


PERLMUTTER_CONFIG = {
    "center": "nersc",
    "host": "perlmutter.nersc.gov",
    "user": "alice",
    "auth": {
        "method": "sshproxy",
        "ssh_config_host": "perlmutter",
    },
    "scheduler": {"type": "slurm", "submit": "sbatch"},
}


class PerlmutterPreflightTests(unittest.TestCase):
    def test_sshproxy_uses_direct_key_probe_without_controlmaster(self) -> None:
        certificate = {"present": True, "valid": True, "detail": "current"}
        with (
            patch.object(server, "_ohmic_reachable", return_value={"reachable": True}),
            patch.object(server.m, "get_machine", return_value=PERLMUTTER_CONFIG),
            patch.object(server, "_tcp_reachable", return_value=True),
            patch.object(
                server.ssh_transport,
                "certificate_status",
                return_value=certificate,
            ),
            patch.object(
                server.ssh_transport,
                "direct_key_channel_usable",
                return_value=(True, "direct key connection usable"),
            ) as direct,
            patch.object(server, "_control_socket_alive") as control,
        ):
            result = server.preflight("perlmutter")["machines"]["perlmutter"]

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["control_master"])
        self.assertTrue(result["direct_key_connection"]["usable"])
        direct.assert_called_once_with("perlmutter")
        control.assert_not_called()

    def test_expired_certificate_blocks_without_remote_probe(self) -> None:
        certificate = {"present": True, "valid": False, "detail": "expired"}
        with (
            patch.object(server, "_ohmic_reachable", return_value={"reachable": True}),
            patch.object(server.m, "get_machine", return_value=PERLMUTTER_CONFIG),
            patch.object(server, "_tcp_reachable", return_value=True),
            patch.object(
                server.ssh_transport,
                "certificate_status",
                return_value=certificate,
            ),
            patch.object(
                server.ssh_transport, "direct_key_channel_usable"
            ) as direct,
        ):
            result = server.preflight("perlmutter")["machines"]["perlmutter"]

        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["direct_key_connection"])
        self.assertTrue(any("refresh" in item for item in result["next_actions"]))
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
