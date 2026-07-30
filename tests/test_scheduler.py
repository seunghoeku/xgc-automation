from __future__ import annotations

import unittest

from tools import scheduler as sched


class PBSSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = sched.PBSScheduler()

    def test_commands_and_full_job_id(self) -> None:
        job_id = "12345.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"
        self.assertEqual(
            self.scheduler.parse_job_id("module banner\n" + job_id + "\n"),
            job_id,
        )
        self.assertEqual(
            self.scheduler.status_argv(job_id),
            ["qstat", "-x", "-f", job_id],
        )
        self.assertEqual(self.scheduler.cancel_argv(job_id), ["qdel", job_id])

    def test_array_job_id_is_accepted(self) -> None:
        self.assertEqual(
            self.scheduler.parse_job_id("12345[].polaris-pbs-01\n"),
            "12345[].polaris-pbs-01",
        )

    def test_invalid_qsub_output_is_rejected(self) -> None:
        with self.assertRaisesRegex(sched.SchedulerError, "could not parse"):
            self.scheduler.parse_job_id("qsub warning only\n")

    def test_active_states_are_normalized(self) -> None:
        for raw_state, expected in (
            ("Q", sched.PENDING),
            ("H", sched.PENDING),
            ("R", sched.RUNNING),
            ("E", sched.RUNNING),
            ("B", sched.RUNNING),
            ("S", sched.RUNNING),
        ):
            with self.subTest(raw_state=raw_state):
                output = f"    job_state = {raw_state}\n"
                self.assertEqual(
                    self.scheduler.parse_status(output, 0),
                    expected,
                )

    def test_finished_zero_exit_is_completed(self) -> None:
        output = "    job_state = F\n    Exit_status = 0\n"
        self.assertEqual(
            self.scheduler.parse_status(output, 0),
            sched.COMPLETED,
        )
        self.assertEqual(self.scheduler.parse_exit_code(output), "0")

    def test_finished_nonzero_exit_is_failed(self) -> None:
        output = "    job_state = F\n    Exit_status = 42\n"
        self.assertEqual(
            self.scheduler.parse_status(output, 0),
            sched.FAILED,
        )
        self.assertEqual(self.scheduler.parse_exit_code(output), "42")

    def test_terminal_state_without_exit_status_is_unknown(self) -> None:
        self.assertEqual(
            self.scheduler.parse_status("    job_state = F\n", 0),
            sched.UNKNOWN,
        )

    def test_deleted_job_is_cancelled(self) -> None:
        output = (
            "    job_state = F\n"
            "    Exit_status = 271\n"
            "    comment = Job deleted by user@host\n"
        )
        self.assertEqual(
            self.scheduler.parse_status(output, 0),
            sched.CANCELLED,
        )

    def test_unparseable_terminal_exit_is_unknown(self) -> None:
        output = "    job_state = F\n    Exit_status = unavailable\n"
        self.assertEqual(
            self.scheduler.parse_status(output, 0),
            sched.UNKNOWN,
        )

    def test_required_pbs_resources_are_validated(self) -> None:
        valid = {
            "account": "TokamakProfile",
            "queue": "debug-scaling",
            "filesystems": "home:grand",
            "system": "polaris",
        }
        self.scheduler.validate_resources(valid)

        for missing in valid:
            invalid = dict(valid)
            invalid[missing] = None
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(sched.SchedulerError, missing):
                    self.scheduler.validate_resources(invalid)


class SchedulerFactoryTests(unittest.TestCase):
    def test_placeholder_scheduler_is_rejected(self) -> None:
        with self.assertRaisesRegex(sched.SchedulerError, "placeholder"):
            sched.get_scheduler({"scheduler": {"type": "<slurm|pbs>"}})

    def test_known_scheduler_is_constructed(self) -> None:
        self.assertIsInstance(
            sched.get_scheduler({"scheduler": {"type": "pbs"}}),
            sched.PBSScheduler,
        )


if __name__ == "__main__":
    unittest.main()
