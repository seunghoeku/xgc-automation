from __future__ import annotations

import tempfile
import unittest
import io
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import queue_watch
from tools import queue_menu
from tools import scheduler as sched
from tools import ssh


SQUEUE_LINES = (
    "57769800|n632pe|PENDING|0:00|1-00:00:00|128|(Priority)|/pscratch/a\n"
    "57795329|tcv_nt|RUNNING|1:02:03|1-00:00:00|128||/pscratch/b\n"
)

# Captured from Polaris (PBS Pro 2026.1) on 2026-09-08: a running job with a
# chained restart held behind it, which is the ordinary shape of a long XGC
# run. Trimmed to the keys the adapter reads; the values are verbatim.
QSELECT_LINES = (
    "7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov\n"
    "7600027.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov\n"
)

QSTAT_JSON = """{
    "timestamp": 1788888532,
    "pbs_version": "2026.1.0.20260730090341",
    "pbs_server": "polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov",
    "Jobs": {
        "7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov": {
            "Job_Name": "n649po",
            "job_state": "R",
            "queue": "medium",
            "Output_Path": "polaris-login-01.hsn.cm.polaris.alcf.anl.gov:/lus/grand/projects/TokamakProfile/shku/n649po_dAdt/n649po.o7600026",
            "jobdir": "/home/shku",
            "depend": "beforeany:7600027.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov@polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov",
            "Hold_Types": "n",
            "comment": "Job run at Tue Sep 08 at 16:51 on (x3103c0s13b0n0:ncpus=64)",
            "Resource_List": {"nodect": 32, "walltime": "06:00:00", "ncpus": 2048},
            "resources_used": {"walltime": "00:36:20", "ncpus": 2048}
        },
        "7600027.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov": {
            "Job_Name": "n649po",
            "job_state": "H",
            "queue": "medium",
            "Output_Path": "polaris-login-01.hsn.cm.polaris.alcf.anl.gov:/lus/grand/projects/TokamakProfile/shku/n649po_dAdt/n649po.o7600027",
            "depend": "afterany:7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov@polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov",
            "Hold_Types": "d",
            "Resource_List": {"nodect": 32, "walltime": "06:00:00", "ncpus": 2048}
        }
    }
}"""


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

    def test_finished_history_survives_quiet_polls_with_request_metadata(self) -> None:
        self._watch({"1": job("1", sched.RUNNING, name="physics", nodes="32",
                              time_limit="06:00:00")})
        with patch.object(queue_watch, "_final_states",
                          return_value={"1": (sched.TIMEOUT, "0:0")}) as final:
            ended = self._watch({})
            quiet = self._watch({})
        final.assert_called_once_with("perlmutter", ["1"])
        self.assertEqual(ended["recent_finished"], quiet["recent_finished"])
        self.assertEqual(quiet["changes"], [])
        recent = quiet["recent_finished"][0]
        self.assertEqual((recent["state"], recent["name"], recent["nodes"],
                          recent["time_limit"]),
                         (sched.TIMEOUT, "physics", "32", "06:00:00"))
        self.assertNotIn("raw_state", recent)

    def test_unknown_accounting_retries_without_losing_departure_time(self) -> None:
        self._watch({"1": job("1", sched.RUNNING)})
        with patch.object(queue_watch, "_final_states", side_effect=ssh.SSHError("offline")):
            unknown = self._watch({})["recent_finished"][0]
        self.assertEqual(unknown["state"], sched.UNKNOWN)
        with patch.object(queue_watch, "_final_states",
                          return_value={"1": (sched.COMPLETED, "0")}) as final:
            resolved = self._watch({})
        final.assert_called_once_with("perlmutter", ["1"])
        self.assertEqual(resolved["recent_finished"][0]["left_queue_at"],
                         unknown["left_queue_at"])
        self.assertEqual(resolved["recent_finished"][0]["state"], sched.COMPLETED)
        self.assertIn("RESOLVED", resolved["changes"][0])

    def test_first_empty_poll_does_not_search_past_jobs(self) -> None:
        with patch.object(queue_watch, "_final_states") as final:
            self.assertEqual(self._watch({})["recent_finished"], [])
        final.assert_not_called()

    def test_history_is_bounded_and_newest_departures_first(self) -> None:
        for i in range(7):
            self._watch({str(i): job(str(i), sched.RUNNING)})
            with patch.object(queue_watch, "_final_states",
                              return_value={str(i): (sched.COMPLETED, "0")}):
                latest = self._watch({})
        self.assertEqual([j["job_id"] for j in latest["recent_finished"]],
                         ["6", "5", "4", "3", "2"])

    def test_requeued_job_is_removed_from_finished_history(self) -> None:
        self._watch({"1": job("1", sched.RUNNING)})
        with patch.object(queue_watch, "_final_states", return_value={}):
            self._watch({})
        self.assertEqual(self._watch({"1": job("1", sched.PENDING)})["recent_finished"], [])

    def test_swiftbar_cli_does_not_poll_or_write(self) -> None:
        self._watch({"1": job("1", sched.RUNNING)})
        before = self.state.read_bytes()
        with patch.object(queue_watch, "watch_many") as poll, redirect_stdout(io.StringIO()) as out:
            code = queue_watch.main(["--swiftbar", "--machine", "perlmutter",
                                     "--state-file", str(self.state)])
        poll.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("128 nodes", out.getvalue())
        self.assertEqual(before, self.state.read_bytes())


class PBSUserQueueTests(unittest.TestCase):
    """The PBS path listing a whole user queue, against captured real output."""

    def setUp(self) -> None:
        self.adapter = sched.PBSScheduler()

    def _jobs(self) -> dict[str, dict[str, Any]]:
        jobs = {j["job_id"]: j for j in self.adapter.parse_user_queue(QSELECT_LINES)}
        detail = self.adapter.parse_user_queue_detail(QSTAT_JSON)
        for job_id, fields in detail.items():
            jobs[job_id].update(fields)
        return jobs

    def test_ids_are_fetched_with_qselect_not_the_truncating_table(self) -> None:
        # qstat -u truncates the job id at a column width and marks the loss
        # with "*", which cannot be used as a key or passed back to qstat.
        self.assertEqual(self.adapter.user_queue_argv("shku"), ["qselect", "-u", "shku"])

    def test_whole_ids_survive_with_a_short_display_form(self) -> None:
        jobs = self.adapter.parse_user_queue(QSELECT_LINES)
        self.assertEqual(
            jobs[0]["job_id"], "7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"
        )
        self.assertEqual(jobs[0]["display_id"], "7600026")

    def test_empty_queue_yields_no_jobs_and_no_detail_call(self) -> None:
        self.assertEqual(self.adapter.parse_user_queue(""), [])
        self.assertIsNone(self.adapter.user_queue_detail_argv([]))

    def test_detail_pass_requests_structured_output_for_named_ids(self) -> None:
        argv = self.adapter.user_queue_detail_argv(["7600026", "7600027"])
        self.assertEqual(argv[:5], ["qstat", "-f", "-F", "json", "7600026"])

    def test_detail_pass_fills_state_nodes_and_walltime(self) -> None:
        running = self._jobs()["7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"]
        self.assertEqual(running["name"], "n649po")
        self.assertEqual(running["state"], sched.RUNNING)
        self.assertEqual(running["raw_state"], "R")
        self.assertEqual(running["nodes"], "32")
        self.assertEqual(running["elapsed"], "00:36:20")
        self.assertEqual(running["time_limit"], "06:00:00")
        self.assertEqual(running["queue"], "medium")

    def test_work_dir_comes_from_the_output_path_without_its_host(self) -> None:
        running = self._jobs()["7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"]
        self.assertEqual(
            running["work_dir"],
            "/lus/grand/projects/TokamakProfile/shku/n649po_dAdt",
        )

    def test_running_job_reports_no_reason(self) -> None:
        running = self._jobs()["7600026.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"]
        self.assertEqual(running["reason"], "")

    def test_dependency_hold_names_the_job_it_waits_for(self) -> None:
        # A chained restart is normal operation, so the report has to say what
        # the hold is rather than leaving it looking stuck.
        held = self._jobs()["7600027.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov"]
        self.assertEqual(held["state"], sched.PENDING)
        self.assertEqual(held["raw_state"], "H")
        self.assertEqual(held["reason"], "dependency hold; afterany:7600026")

    def test_scheduler_comment_explains_a_plain_queued_job(self) -> None:
        detail = self.adapter.parse_user_queue_detail(
            '{"Jobs": {"9.srv": {"job_state": "Q", "Hold_Types": "n",'
            ' "comment": "Not Running: Insufficient amount of resource"}}}'
        )
        self.assertEqual(
            detail["9.srv"]["reason"], "Not Running: Insufficient amount of resource"
        )

    def test_unparseable_json_is_an_error_not_an_empty_queue(self) -> None:
        # Reporting "no jobs" for a machine that has jobs is the failure this
        # adapter exists to avoid.
        with self.assertRaises(sched.SchedulerError):
            self.adapter.parse_user_queue_detail("qstat: Unknown Job Id")

    def test_finished_job_is_resolved_through_extended_status(self) -> None:
        self.assertEqual(
            self.adapter.accounting_argv("7600026"),
            ["qstat", "-x", "-f", "7600026"],
        )
        state, exit_code = self.adapter.parse_accounting(
            "job_state = F\nExit_status = 0\n"
        )
        self.assertEqual((state, exit_code), (sched.COMPLETED, "0"))


class ProbeTests(unittest.TestCase):
    """Reachability is decided before the poll, and never authenticates."""

    def _probe(
        self,
        *,
        target: Any = None,
        connect_error: Exception | None = None,
        channel: tuple[bool, str] = (True, "ok"),
        scheduler_error: Exception | None = None,
    ) -> tuple[dict[str, Any], Any]:
        target = target or ssh.SSHTarget("stellar", "ssh_controlmaster")
        with patch.object(
            queue_watch.machine_config, "get_machine", return_value={}
        ), patch.object(
            queue_watch.sched,
            "get_scheduler",
            side_effect=scheduler_error,
            return_value=sched.SlurmScheduler(),
        ), patch.object(
            queue_watch, "_user_for", return_value="sku"
        ), patch.object(
            queue_watch.ssh, "host_for", return_value=target
        ), patch.object(
            queue_watch.ssh, "connection_options", side_effect=connect_error
        ), patch.object(
            queue_watch.ssh, "command_channel_usable", return_value=channel
        ) as channel_probe:
            return queue_watch.probe("stellar"), channel_probe

    def test_live_session_is_pollable(self) -> None:
        status, _ = self._probe()
        self.assertTrue(status["pollable"])

    def test_unknown_machine_is_skipped_not_raised(self) -> None:
        with patch.object(
            queue_watch.machine_config,
            "get_machine",
            side_effect=KeyError("unknown machine: 'nope'"),
        ):
            status = queue_watch.probe("nope")
        self.assertFalse(status["pollable"])
        self.assertIn("unknown machine", status["detail"])

    def test_unconfirmed_scheduler_is_skipped_with_its_reason(self) -> None:
        # flux still carries a <slurm|pbs> placeholder in machines.yaml.
        status, _ = self._probe(
            scheduler_error=sched.SchedulerError("scheduler.type is still a placeholder")
        )
        self.assertFalse(status["pollable"])
        self.assertIn("placeholder", status["detail"])

    def test_expired_credential_is_reported_as_the_reason(self) -> None:
        status, _ = self._probe(
            connect_error=ssh.SSHError("SSH certificate for 'perlmutter' is expired")
        )
        self.assertFalse(status["pollable"])
        self.assertIn("certificate", status["detail"])

    def test_live_master_that_cannot_open_a_channel_is_skipped(self) -> None:
        status, _ = self._probe(channel=(False, "command channel timed out"))
        self.assertFalse(status["pollable"])
        self.assertIn("cannot open a command channel", status["detail"])

    def test_sshproxy_host_needs_no_channel_probe(self) -> None:
        # Its certificate check is local, so a round trip here would only add
        # latency to every poll.
        status, channel_probe = self._probe(
            target=ssh.SSHTarget("perlmutter", "sshproxy")
        )
        self.assertTrue(status["pollable"])
        channel_probe.assert_not_called()


class WatchManyTests(unittest.TestCase):
    """One unreachable machine must not stop the others."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(
        self,
        probes: dict[str, bool],
        jobs: dict[str, Any] | None = None,
        poll_error: Exception | None = None,
    ) -> tuple[dict[str, Any], Any]:
        def fake_probe(machine: str) -> dict[str, Any]:
            return {
                "machine": machine,
                "pollable": probes[machine],
                "detail": "ready" if probes[machine] else "no live ssh session",
            }

        with patch.object(queue_watch, "probe", side_effect=fake_probe), patch.object(
            queue_watch, "poll", side_effect=poll_error, return_value=jobs or {}
        ), patch.object(
            queue_watch, "_certificate_note", return_value=None
        ), patch.object(queue_watch, "notify") as notified:
            payload = queue_watch.watch_many(
                sorted(probes),
                state_files={
                    m: Path(self.tmp.name) / f"{m}.json" for m in probes
                },
            )
        return payload, notified

    def test_unreachable_machine_is_skipped_and_the_rest_are_polled(self) -> None:
        payload, _ = self._run(
            {"polaris": True, "stellar": False}, {"1": job("1", sched.PENDING)}
        )
        self.assertEqual(payload["polled"], ["polaris"])
        self.assertEqual(payload["skipped"], ["stellar"])
        self.assertEqual(payload["queued"], 1)

    def test_skipping_is_normal_operation_not_a_failure(self) -> None:
        payload, _ = self._run(
            {"polaris": True, "stellar": False}, {"1": job("1", sched.PENDING)}
        )
        self.assertEqual(
            queue_watch.exit_code_for(payload, ["polaris", "stellar"]),
            queue_watch.EXIT_OK,
        )

    def test_being_blind_to_every_machine_is_a_failure(self) -> None:
        payload, _ = self._run({"perlmutter": False, "stellar": False})
        self.assertEqual(
            queue_watch.exit_code_for(payload, ["perlmutter", "stellar"]),
            queue_watch.EXIT_UNREACHABLE,
        )

    def test_a_poll_that_breaks_is_reported_as_an_error(self) -> None:
        payload, _ = self._run(
            {"polaris": True}, poll_error=ssh.SSHError("remote command failed")
        )
        self.assertEqual(payload["failed"], ["polaris"])
        self.assertEqual(
            queue_watch.exit_code_for(payload, ["polaris"]), queue_watch.EXIT_ERROR
        )

    def test_changes_are_notified_once_and_name_their_machine(self) -> None:
        payload, notified = self._run(
            {"polaris": True, "stellar": True}, {"1": job("1", sched.PENDING)}
        )
        self.assertEqual(notified.call_count, 1)
        body = notified.call_args.args[1]
        self.assertIn("polaris: NEW", body)
        self.assertIn("stellar: NEW", body)
        self.assertEqual(len(payload["changes"]), 2)

    def test_a_quiet_poll_raises_no_notification(self) -> None:
        probes = {"polaris": True}
        jobs = {"1": job("1", sched.PENDING)}
        self._run(probes, jobs)
        _, notified = self._run(probes, jobs)
        notified.assert_not_called()

    def test_unreachable_machine_keeps_last_successful_queue(self) -> None:
        self._run({"polaris": True}, {"1": job("1", sched.RUNNING)})
        path = Path(self.tmp.name) / "polaris.json"
        previous = queue_watch.load_state(path)
        self._run({"polaris": False})
        saved = queue_watch.load_state(path)
        self.assertEqual(saved["jobs"], previous["jobs"])
        self.assertEqual(saved["polled_at"], previous["polled_at"])
        self.assertEqual(saved["connection"], "skipped")


class MenuTests(unittest.TestCase):
    NOW = datetime.fromisoformat("2026-09-16T12:00:00-04:00")

    def test_offline_and_stale_queues_are_not_counted_as_live(self) -> None:
        snapshots = [
            {"machine": "online", "polled_at": self.NOW.isoformat(),
             "jobs": {"1": job("1", sched.RUNNING)}},
            {"machine": "offline", "polled_at": self.NOW.isoformat(),
             "connection": "skipped", "detail": "expired", "jobs": {"2": job("2", sched.RUNNING)}},
            {"machine": "old", "polled_at": (self.NOW - timedelta(hours=1)).isoformat(),
             "jobs": {"3": job("3", sched.PENDING)}},
        ]
        rendered = queue_menu.render(snapshots, now=self.NOW)
        self.assertEqual(rendered.splitlines()[0], "HPC · 실행 1 · 대기 0 · ⚠ 2")
        self.assertIn("연결 확인 필요: expired", rendered)
        self.assertIn("마지막 확인 큐", rendered)

    def test_finished_job_has_name_nodes_requested_time_and_observed_date(self) -> None:
        finished = job("4", sched.TIMEOUT, name="test-run", nodes="32",
                       time_limit="06:00:00", left_queue_at=self.NOW.isoformat())
        rendered = queue_menu.render([{"machine": "pbs", "recent_finished": [finished]}])
        self.assertIn("시간 제한 종료 · test-run · #4 · 32 nodes · 요청 06:00:00", rendered)
        self.assertIn("큐 이탈 확인:", rendered)

    def test_scheduler_text_cannot_inject_menu_actions(self) -> None:
        unsafe = job("1", sched.RUNNING, name="name\n---\nattack | bash=/bin/rm")
        lines = queue_menu.job_lines(unsafe)
        self.assertNotIn("|", "\n".join(lines))
        self.assertNotIn("\n", lines[0])

    def test_missing_cache_is_unknown_instead_of_empty_live_queue(self) -> None:
        rendered = queue_menu.render([{"machine": "new"}], now=self.NOW)
        self.assertIn("확인 기록 없음", rendered)
        self.assertIn("조회 기록 없음", rendered)


class RotateLogTests(unittest.TestCase):
    """Weekly rotation of the watcher's own log, which is also its open stdout."""

    NOW = datetime.fromisoformat("2026-09-08T12:00:00-04:00")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "xgc-queue-watch.log"
        self.archive = Path(self.tmp.name) / "xgc-queue-watch.log.1"
        self.marker = Path(self.tmp.name) / "rotated-at"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rotate(self, now: datetime | None = None, days: int = 7) -> bool:
        return queue_watch.rotate_log(
            self.log,
            interval=timedelta(days=days),
            now=now or self.NOW,
            marker=self.marker,
        )

    def _mark(self, stamp: str) -> None:
        self.marker.write_text(stamp, encoding="utf-8")

    def test_a_log_of_unknown_age_is_archived_and_dated(self) -> None:
        # No marker: the live file's period cannot be established, so it is
        # closed out rather than left to grow forever.
        self.log.write_text("old week\n", encoding="utf-8")
        self.assertTrue(self._rotate())
        self.assertEqual(self.archive.read_text(encoding="utf-8"), "old week\n")
        self.assertFalse(self.log.exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), self.NOW.isoformat())

    def test_nothing_moves_before_the_week_is_up(self) -> None:
        self._mark("2026-09-05T12:00:00-04:00")
        self.log.write_text("this week\n", encoding="utf-8")
        self.assertFalse(self._rotate())
        self.assertEqual(self.log.read_text(encoding="utf-8"), "this week\n")
        self.assertFalse(self.archive.exists())

    def test_the_week_boundary_rotates(self) -> None:
        self._mark("2026-09-01T12:00:00-04:00")
        self.log.write_text("closing week\n", encoding="utf-8")
        self.assertTrue(self._rotate())
        self.assertEqual(self.archive.read_text(encoding="utf-8"), "closing week\n")

    def test_the_previous_archive_is_discarded_not_accumulated(self) -> None:
        # Two generations, always: the live file and one archive.
        self.archive.write_text("two weeks ago\n", encoding="utf-8")
        self._mark("2026-09-01T12:00:00-04:00")
        self.log.write_text("last week\n", encoding="utf-8")

        self.assertTrue(self._rotate())
        self.assertEqual(self.archive.read_text(encoding="utf-8"), "last week\n")
        self.assertEqual(
            sorted(f.name for f in Path(self.tmp.name).iterdir()),
            ["rotated-at", "xgc-queue-watch.log.1"],
        )

    def test_the_clock_restarts_so_the_next_week_is_a_full_one(self) -> None:
        self._mark("2026-09-01T12:00:00-04:00")
        self.log.write_text("last week\n", encoding="utf-8")
        self.assertTrue(self._rotate())

        self.log.write_text("new week\n", encoding="utf-8")
        self.assertFalse(self._rotate(now=self.NOW + timedelta(days=6)))
        self.assertTrue(self._rotate(now=self.NOW + timedelta(days=7)))

    def test_a_missing_log_is_dated_without_claiming_a_rotation(self) -> None:
        self.assertFalse(self._rotate())
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), self.NOW.isoformat())

    def test_an_unreadable_marker_is_treated_as_due(self) -> None:
        self._mark("not a timestamp")
        self.log.write_text("orphaned\n", encoding="utf-8")
        self.assertTrue(self._rotate())
        self.assertEqual(self.archive.read_text(encoding="utf-8"), "orphaned\n")

    def test_an_open_stdout_follows_the_file_into_the_archive(self) -> None:
        # This is why rotation runs last. Under launchd the log is stdout,
        # opened on that inode before the process starts, so writes follow the
        # rename: the rotating poll's report lands at the end of the archive
        # instead of being lost, and the next poll gets a fresh file.
        self.log.write_text("earlier this week\n", encoding="utf-8")
        with self.log.open("a", encoding="utf-8") as stdout_fd:
            self.assertTrue(self._rotate())
            stdout_fd.write("the rotating poll\n")

        self.assertEqual(
            self.archive.read_text(encoding="utf-8"),
            "earlier this week\nthe rotating poll\n",
        )
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
