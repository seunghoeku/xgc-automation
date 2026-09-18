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

    def test_walltime_kill_is_timeout_not_failed(self) -> None:
        output = (
            "    job_state = F\n"
            "    resources_used.walltime = 06:00:59\n"
            "    Exit_status = -29\n"
        )
        self.assertEqual(
            self.scheduler.parse_status(output, 0),
            sched.TIMEOUT,
        )
        self.assertEqual(self.scheduler.parse_exit_code(output), "-29")

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
            "account": "SomeProject",
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


class SlurmStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scheduler = sched.SlurmScheduler()

    def test_wall_clock_timeout_is_its_own_state(self) -> None:
        # A long XGC run is routinely sized to hit the wall clock and restart
        # from its checkpoint. Reporting that as FAILED would turn normal
        # operation into an error, so TIMEOUT stays distinguishable.
        for raw in ("TIMEOUT", "TO"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.scheduler.parse_status(f"{raw}\n", 0), sched.TIMEOUT
                )
        self.assertNotEqual(sched.TIMEOUT, sched.FAILED)

    def test_timeout_still_ends_the_job(self) -> None:
        self.assertIn(sched.TIMEOUT, sched.TERMINAL_STATES)

    def test_real_failures_remain_failures(self) -> None:
        for raw in ("FAILED", "NODE_FAIL", "OUT_OF_MEMORY"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.scheduler.parse_status(f"{raw}\n", 0), sched.FAILED
                )

    def test_accounting_reports_timeout_after_the_job_leaves_the_queue(self) -> None:
        # squeue forgets finished jobs, so a timed-out run is normally
        # discovered through sacct rather than the queue.
        state, exit_code = self.scheduler.parse_accounting("TIMEOUT|0:1\n")
        self.assertEqual(state, sched.TIMEOUT)
        self.assertEqual(exit_code, "0:1")


class SlurmSchedulerValidationTests(unittest.TestCase):
    """Placeholders must be blocked before a Slurm script is rendered.

    sbatch accepts `--account=<nersc_account>` textually; mail_user with a
    placeholder even *succeeds* silently. The adapter must catch both.
    """

    def setUp(self) -> None:
        self.scheduler = sched.SlurmScheduler()

    def test_filled_or_absent_values_pass(self) -> None:
        # Absent optional fields are fine (partition-less sites exist); only
        # unfilled <placeholders> are configuration errors.
        self.scheduler.validate_resources({})
        self.scheduler.validate_resources(
            {"account": "m0000", "mail_user": "alice@example.org",
             "partition": None, "qos": "debug"}
        )

    def test_placeholder_values_are_rejected_by_name(self) -> None:
        cases = {
            "account": "<nersc_account>",
            "partition": "<partition>",
            "qos": "<qos>",
            "constraint": "<constraint>",
            "mail_user": "<email>",
            "mail_type": "<mail_type>",
        }
        for label, value in cases.items():
            with self.subTest(field=label):
                with self.assertRaisesRegex(sched.SchedulerError, label):
                    self.scheduler.validate_resources({label: value})


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
