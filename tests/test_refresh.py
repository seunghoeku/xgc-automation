from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import refresh as refresh_tool
from tools import scheduler as sched
from tools import simulation_catalog as sc
from tools import ssh


class FakeScheduler:
    """Canned scheduler answers, keyed by job id.

    Refresh must be testable without a cluster, and each test needs to control
    exactly what the scheduler reports.
    """

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, machine: str, job_id: str) -> dict[str, Any]:
        self.calls.append((machine, job_id))
        answer = self.answers.get(job_id)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return {"state": sched.UNKNOWN, "terminal": False}
        return answer


def finished(state: str, exit_code: int | None = 0) -> dict[str, Any]:
    return {"state": state, "exit_code": exit_code, "terminal": True}


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, record_id: str = "n645pe", **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("machines", ["perlmutter"])
        kwargs.setdefault("short_name", "tcv")
        return sc.create_record(record_id, catalog_dir=self.dir, **kwargs)

    def attach(self, record_id: str, run_id: str, job_id: str, **kwargs: Any) -> None:
        run = {
            "run_id": run_id,
            "machine": "perlmutter",
            "job_id": job_id,
            "submitted_at": kwargs.pop("submitted_at", f"2026-08-01T00:00:0{job_id}Z"),
        }
        run.update(kwargs)
        sc.attach_run(record_id, run, catalog_dir=self.dir)

    def refresh(self, answers: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        fake = FakeScheduler(answers)
        result = refresh_tool.refresh(
            catalog_dir=self.dir, status_fn=fake, **kwargs
        )
        result["_calls"] = fake.calls
        return result

    def status_of(self, record_id: str = "n645pe") -> str:
        return sc.read_record(record_id, catalog_dir=self.dir)["status"]

    # -- what gets polled ------------------------------------------------

    def test_finished_simulations_are_not_polled(self) -> None:
        self.make("n645pe", status="archived")
        self.attach("n645pe", "run-a", "1")
        result = self.refresh({"1": finished("completed")})
        self.assertEqual(result["_calls"], [])

    def test_settled_runs_are_not_polled_again(self) -> None:
        # Re-asking about a job that already reported is a wasted SSH round
        # trip, and refresh has to stay cheap as the catalog grows.
        self.make()
        self.attach("n645pe", "run-a", "1", state="completed")
        self.attach("n645pe", "run-b", "2")
        result = self.refresh({"2": finished("completed")})
        self.assertEqual(result["_calls"], [("perlmutter", "2")])

    def test_runs_without_a_job_id_are_skipped(self) -> None:
        self.make()
        sc.attach_run("n645pe", {"run_id": "run-a"}, catalog_dir=self.dir)
        result = self.refresh({})
        self.assertEqual(result["_calls"], [])

    def test_machine_filter_limits_the_polling(self) -> None:
        self.make("n645pe", machines=["perlmutter"])
        self.attach("n645pe", "run-a", "1")
        result = self.refresh({"1": finished("completed")}, machine="polaris")
        self.assertEqual(result["_calls"], [])

    def test_record_filter_limits_the_polling(self) -> None:
        self.make("n645pe")
        self.attach("n645pe", "run-a", "1")
        self.make("n646pe")
        self.attach("n646pe", "run-b", "2")
        result = self.refresh({"1": finished("completed")}, record_id="n645pe")
        self.assertEqual(result["_calls"], [("perlmutter", "1")])

    def test_plan_contacts_nothing(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1")
        plan = refresh_tool.plan_refresh(catalog_dir=self.dir)
        self.assertEqual(plan["counts"]["runs"], 1)
        self.assertFalse(plan["writes"])
        self.assertEqual(self.status_of(), "submitted")

    # -- timeout is not failure -------------------------------------------

    def test_wall_clock_timeout_is_interrupted_not_failed(self) -> None:
        # XGC is routinely sized to hit the wall clock and restart from its
        # checkpoint. Calling that a failure would report normal operation as
        # an error.
        self.make()
        self.attach("n645pe", "run-a", "1")
        self.refresh({"1": finished("timeout", exit_code=None)})
        self.assertEqual(self.status_of(), "interrupted")

    def test_timeout_then_restart_that_finishes_is_completed(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1", submitted_at="2026-08-01T00:00:00Z")
        self.attach("n645pe", "run-b", "2", submitted_at="2026-08-02T00:00:00Z")
        self.refresh({"1": finished("timeout", None), "2": finished("completed")})
        self.assertEqual(self.status_of(), "completed")

    def test_interrupted_simulation_is_still_polled_later(self) -> None:
        # An interrupted run is normally followed by a restart whose state
        # still needs collecting, so it must stay in the active set.
        self.make()
        self.attach("n645pe", "run-a", "1")
        self.refresh({"1": finished("timeout", None)})
        self.assertEqual(self.status_of(), "interrupted")

        self.attach("n645pe", "run-b", "2", submitted_at="2026-09-01T00:00:00Z")
        result = self.refresh({"2": finished("completed")})

        self.assertEqual(result["_calls"], [("perlmutter", "2")])
        self.assertEqual(self.status_of(), "completed")

    # -- restart chains ----------------------------------------------------

    def test_early_failure_later_restarted_and_finished_is_completed(self) -> None:
        # The simulation's state is where the chain ended up, not the worst
        # thing that ever happened in it.
        self.make()
        self.attach("n645pe", "run-a", "1", submitted_at="2026-08-01T00:00:00Z")
        self.attach("n645pe", "run-b", "2", submitted_at="2026-08-02T00:00:00Z")
        self.refresh({"1": finished("failed", 1), "2": finished("completed")})
        self.assertEqual(self.status_of(), "completed")

    def test_failure_after_a_success_is_failed(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1", submitted_at="2026-08-01T00:00:00Z")
        self.attach("n645pe", "run-b", "2", submitted_at="2026-08-02T00:00:00Z")
        self.refresh({"1": finished("completed"), "2": finished("failed", 1)})
        self.assertEqual(self.status_of(), "failed")

    def test_poll_order_does_not_decide_the_status(self) -> None:
        # Polling happens in whatever order the runs are stored; the newest
        # run must win regardless.
        self.make()
        self.attach("n645pe", "run-new", "9", submitted_at="2026-08-09T00:00:00Z")
        self.attach("n645pe", "run-old", "1", submitted_at="2026-08-01T00:00:00Z")
        self.refresh({"9": finished("completed"), "1": finished("failed", 1)})
        self.assertEqual(self.status_of(), "completed")

    def test_a_running_job_anywhere_makes_the_simulation_running(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1", submitted_at="2026-08-01T00:00:00Z")
        self.attach("n645pe", "run-b", "2", submitted_at="2026-08-02T00:00:00Z")
        self.refresh(
            {
                "1": finished("completed"),
                "2": {"state": "running", "terminal": False},
            }
        )
        self.assertEqual(self.status_of(), "running")

    def test_restart_submission_does_not_undo_a_human_conclusion(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1")
        sc.set_archive(
            "n645pe", system="hpss", path="/home/s/sku/n645pe.tar", catalog_dir=self.dir
        )
        self.attach("n645pe", "run-b", "2")
        self.assertEqual(self.status_of(), "archived")

    def test_scheduler_result_does_not_undo_a_human_conclusion(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1")
        sc.update_record("n645pe", {"status": "analyzed"}, catalog_dir=self.dir)
        self.refresh({"1": finished("failed", 1)})
        self.assertEqual(self.status_of(), "analyzed")

    # -- robustness --------------------------------------------------------

    def test_forgotten_job_does_not_erase_a_known_state(self) -> None:
        # squeue and sacct can both come up empty for an old job. Recording
        # "unknown" would replace real information with an absence.
        self.make()
        self.attach("n645pe", "run-a", "1")
        result = self.refresh({})
        self.assertEqual(result["counts"]["updated"], 0)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(self.status_of(), "submitted")

    def test_one_unreachable_machine_does_not_stop_the_rest(self) -> None:
        self.make("n645pe")
        self.attach("n645pe", "run-a", "1")
        self.make("n646pe")
        self.attach("n646pe", "run-b", "2")

        result = self.refresh(
            {"1": ssh.SSHError("credential expired"), "2": finished("completed")}
        )

        self.assertEqual(result["counts"]["errors"], 1)
        self.assertEqual(result["counts"]["updated"], 1)
        self.assertEqual(self.status_of("n646pe"), "completed")

    def test_unchanged_state_is_not_rewritten(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1", state="running")
        result = self.refresh({"1": {"state": "running", "terminal": False}})
        self.assertEqual(result["counts"]["updated"], 0)
        self.assertEqual(result["counts"]["unchanged"], 1)

    def test_refresh_is_idempotent(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1")
        self.refresh({"1": finished("completed")})
        again = self.refresh({"1": finished("completed")})
        self.assertEqual(again["_calls"], [])
        self.assertEqual(self.status_of(), "completed")

    def test_empty_catalog_is_not_an_error(self) -> None:
        result = self.refresh({})
        self.assertEqual(result["counts"]["polled"], 0)
        self.assertEqual(result["counts"]["errors"], 0)

    def test_exit_code_is_recorded_on_the_run(self) -> None:
        self.make()
        self.attach("n645pe", "run-a", "1")
        self.refresh({"1": finished("failed", 2)})
        run = sc.read_record("n645pe", catalog_dir=self.dir)["runs"][0]
        self.assertEqual(run["exit_code"], 2)
        self.assertEqual(run["state"], "failed")


class DeriveStatusTests(unittest.TestCase):
    """derive_status reads the whole restart chain, not one run."""

    @staticmethod
    def record(status: str, *runs: dict[str, Any]) -> dict[str, Any]:
        return {"status": status, "runs": list(runs)}

    @staticmethod
    def a_run(run_id: str, state: str | None, at: str) -> dict[str, Any]:
        return {"run_id": run_id, "state": state, "submitted_at": at}

    def test_no_runs_keeps_the_current_status(self) -> None:
        self.assertEqual(sc.derive_status(self.record("staged")), "staged")

    def test_runs_without_outcomes_are_still_in_flight(self) -> None:
        record = self.record("submitted", self.a_run("a", None, "2026-08-01"))
        self.assertEqual(sc.derive_status(record), "submitted")

    def test_newest_outcome_wins(self) -> None:
        record = self.record(
            "running",
            self.a_run("a", "failed", "2026-08-01"),
            self.a_run("b", "completed", "2026-08-02"),
        )
        self.assertEqual(sc.derive_status(record), "completed")

    def test_a_later_run_without_an_outcome_falls_back_to_the_last_known(self) -> None:
        record = self.record(
            "running",
            self.a_run("a", "completed", "2026-08-01"),
            self.a_run("b", None, "2026-08-02"),
        )
        self.assertEqual(sc.derive_status(record), "completed")

    def test_cancelled_is_not_an_outcome_the_scheduler_can_conclude(self) -> None:
        # Cancelling one attempt says nothing about the science.
        record = self.record("submitted", self.a_run("a", "cancelled", "2026-08-01"))
        self.assertEqual(sc.derive_status(record), "submitted")

    def test_human_judgement_is_never_overwritten(self) -> None:
        for status in ("analyzed", "archived", "abandoned"):
            with self.subTest(status=status):
                record = self.record(status, self.a_run("a", "failed", "2026-08-01"))
                self.assertEqual(sc.derive_status(record), status)


if __name__ == "__main__":
    unittest.main()
