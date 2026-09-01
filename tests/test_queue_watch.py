from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import queue_watch
from tools import scheduler as sched


SQUEUE_LINES = (
    "57769800|n632pe|PENDING|0:00|1-00:00:00|128|(Priority)|/pscratch/a\n"
    "57795329|tcv_nt|RUNNING|1:02:03|1-00:00:00|128||/pscratch/b\n"
)


def job(job_id: str, state: str, name: str = "n632pe", **extra: Any) -> dict[str, Any]:
    base = {
        "job_id": job_id,
        "name": name,
        "state": state,
        "elapsed": "0:00",
        "time_limit": "1-00:00:00",
        "nodes": "128",
        "reason": "(Priority)",
        "work_dir": "/pscratch/a",
    }
    base.update(extra)
    return base


class SlurmUserQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = sched.SlurmScheduler()

    def test_argv_asks_only_about_this_user(self) -> None:
        argv = self.adapter.user_queue_argv("sku")
        self.assertEqual(argv[:3], ["squeue", "-u", "sku"])
        self.assertIn("-h", argv)

    def test_parse_normalizes_state_and_keeps_raw(self) -> None:
        jobs = self.adapter.parse_user_queue(SQUEUE_LINES)
        self.assertEqual([j["job_id"] for j in jobs], ["57769800", "57795329"])
        self.assertEqual(jobs[0]["state"], sched.PENDING)
        self.assertEqual(jobs[0]["raw_state"], "PENDING")
        self.assertEqual(jobs[0]["work_dir"], "/pscratch/a")
        self.assertEqual(jobs[1]["state"], sched.RUNNING)
        self.assertEqual(jobs[1]["elapsed"], "1:02:03")

    def test_parse_skips_malformed_lines(self) -> None:
        self.assertEqual(self.adapter.parse_user_queue("garbage\n\n"), [])

    def test_pbs_reports_that_it_cannot_list_a_queue(self) -> None:
        # Better an explicit gap than an untested qstat parser that quietly
        # reports an empty queue on a machine with jobs in it.
        with self.assertRaises(NotImplementedError):
            sched.PBSScheduler().user_queue_argv("shku")


class DiffTests(unittest.TestCase):
    def test_new_job_is_reported(self) -> None:
        changes = queue_watch.diff_queues(
            "perlmutter", {}, {"1": job("1", sched.PENDING)}
        )
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].startswith("NEW      1 n632pe [pending]"))

    def test_unchanged_queue_is_silent(self) -> None:
        snapshot = {"1": job("1", sched.PENDING)}
        self.assertEqual(
            queue_watch.diff_queues("perlmutter", snapshot, dict(snapshot)), []
        )

    def test_state_transition_is_reported(self) -> None:
        changes = queue_watch.diff_queues(
            "perlmutter",
            {"1": job("1", sched.PENDING)},
            {"1": job("1", sched.RUNNING, elapsed="0:31")},
        )
        self.assertIn("pending -> running", changes[0])

    def test_departed_job_is_resolved_through_accounting(self) -> None:
        with patch.object(
            queue_watch,
            "_final_states",
            return_value={"1": (sched.TIMEOUT, "0:0")},
        ) as finals:
            changes = queue_watch.diff_queues(
                "perlmutter", {"1": job("1", sched.RUNNING)}, {}
            )
        finals.assert_called_once_with("perlmutter", ["1"])
        self.assertIn("FINISHED 1 n632pe [timeout, exit 0:0]", changes[0])

    def test_departed_job_without_accounting_stays_unknown(self) -> None:
        with patch.object(queue_watch, "_final_states", return_value={}):
            changes = queue_watch.diff_queues(
                "perlmutter", {"1": job("1", sched.RUNNING)}, {}
            )
        self.assertIn("[unknown]", changes[0])


class WatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "perlmutter.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _watch(self, jobs: dict[str, Any]) -> dict[str, Any]:
        with patch.object(queue_watch, "poll", return_value=jobs), patch.object(
            queue_watch, "_certificate_note", return_value=None
        ), patch.object(queue_watch, "notify") as notified:
            payload = queue_watch.watch("perlmutter", state_file=self.state)
        payload["notified"] = notified.called
        return payload

    def test_first_poll_reports_then_second_is_quiet(self) -> None:
        first = self._watch({"1": job("1", sched.PENDING)})
        self.assertTrue(first["changes"])
        self.assertTrue(first["notified"])

        second = self._watch({"1": job("1", sched.PENDING)})
        self.assertEqual(second["changes"], [])
        self.assertFalse(second["notified"])

    def test_certificate_warning_is_appended(self) -> None:
        with patch.object(queue_watch, "poll", return_value={}), patch.object(
            queue_watch, "_certificate_note", return_value="expires soon"
        ), patch.object(queue_watch, "notify"):
            payload = queue_watch.watch("perlmutter", state_file=self.state)
        self.assertEqual(payload["changes"], ["NOTE     expires soon"])

    def test_corrupt_state_file_is_treated_as_first_run(self) -> None:
        self.state.write_text("{not json", encoding="utf-8")
        payload = self._watch({"1": job("1", sched.PENDING)})
        self.assertTrue(payload["changes"])


if __name__ == "__main__":
    unittest.main()
