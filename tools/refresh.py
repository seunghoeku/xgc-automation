"""Bring the catalog up to date with the schedulers, in one command.

Submission links a run to its simulation, and ``job_status`` writes the
scheduler's answer back. But that only happens when someone asks about a
specific job id, which means a simulation submitted and then forgotten stays
``submitted`` forever, and catching up on a week of work means digging job ids
out of provenance one at a time.

``refresh`` inverts that: it finds every simulation the catalog still thinks is
in flight, asks each machine about the runs involved, and writes the answers
back. One command, no arguments, safe to repeat -- the kind of thing that can
live in a shell alias or a cron entry.

It is read-only against the machines: it polls the scheduler and never submits,
cancels, or touches remote files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from tools import hpc_runner
from tools import scheduler as sched
from tools import simulation_catalog as catalog
from tools import ssh

# Simulations in these states may still have news from the scheduler. Anything
# else is either finished or a human's conclusion, and polling it would waste
# an SSH round trip per record.
#
# `interrupted` is included: a run that hit the wall clock is usually followed
# by a restart, and that restart's own state still needs collecting.
ACTIVE_STATUSES = ("submitted", "running", "interrupted")

# Run states that can still change. A run already reporting one of the terminal
# states is never polled again, which is what keeps refresh cheap as the
# catalog grows.
_SETTLED_RUN_STATES = frozenset(sched.TERMINAL_STATES)


class RefreshError(RuntimeError):
    """The refresh request cannot be satisfied."""


def _pending_runs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Runs in this simulation whose outcome is not yet known."""
    pending: list[dict[str, Any]] = []
    for run in record.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if not run.get("job_id") or not run.get("machine"):
            # Adopted records sometimes predate job-id capture; nothing to ask.
            continue
        if str(run.get("state") or "") in _SETTLED_RUN_STATES:
            continue
        pending.append(run)
    return pending


def plan_refresh(
    *,
    catalog_dir: Path | None = None,
    machine: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    """List the simulations and runs that would be polled. Contacts nothing."""
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for record in catalog.iter_records(catalog_dir=catalog_dir):
        if record_id and record.get("record_id") != record_id:
            continue
        if record.get("status") not in ACTIVE_STATUSES:
            continue

        pending = _pending_runs(record)
        if machine:
            pending = [run for run in pending if run.get("machine") == machine]
        if not pending:
            skipped.append(
                {
                    "record_id": record.get("record_id"),
                    "status": record.get("status"),
                    "reason": "no run with an outstanding scheduler state",
                }
            )
            continue

        targets.append(
            {
                "record_id": record.get("record_id"),
                "status": record.get("status"),
                "runs": [
                    {
                        "run_id": run.get("run_id"),
                        "machine": run.get("machine"),
                        "job_id": run.get("job_id"),
                        "state": run.get("state"),
                    }
                    for run in pending
                ],
            }
        )

    return {
        "targets": targets,
        "skipped": skipped,
        "counts": {
            "simulations": len(targets),
            "runs": sum(len(item["runs"]) for item in targets),
            "skipped": len(skipped),
        },
        "writes": False,
    }


def refresh(
    *,
    catalog_dir: Path | None = None,
    machine: str | None = None,
    record_id: str | None = None,
    status_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Poll every in-flight run and write the scheduler's answers back.

    Unlike the write operations in this toolchain, this needs no confirmation:
    it only records what the scheduler already decided. There is nothing for a
    human to approve, and requiring approval would defeat the purpose of a
    command meant to be run without thinking about it.

    A machine that is unreachable is reported per-run and does not stop the
    rest, so one expired credential cannot block the whole catalog.
    """
    plan = plan_refresh(catalog_dir=catalog_dir, machine=machine, record_id=record_id)
    query = status_fn or hpc_runner.status

    updated: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for target in plan["targets"]:
        simulation_id = target["record_id"]
        before = target["status"]

        for run in target["runs"]:
            try:
                result = query(run["machine"], str(run["job_id"]))
            except (ssh.SSHError, sched.SchedulerError, KeyError) as exc:
                errors.append(
                    {
                        "record_id": simulation_id,
                        "run_id": run["run_id"],
                        "machine": run["machine"],
                        "job_id": run["job_id"],
                        "error": str(exc),
                    }
                )
                continue

            state = str(result.get("state") or sched.UNKNOWN)
            if state == sched.UNKNOWN:
                # The scheduler has forgotten the job and accounting had
                # nothing either. Recording "unknown" would overwrite a real
                # state with an absence, so leave the run as it is.
                unchanged.append(
                    {
                        "record_id": simulation_id,
                        "run_id": run["run_id"],
                        "reason": "scheduler no longer knows this job",
                    }
                )
                continue
            if state == run.get("state"):
                unchanged.append(
                    {"record_id": simulation_id, "run_id": run["run_id"], "state": state}
                )
                continue

            try:
                record = catalog.update_run_state(
                    simulation_id,
                    run["run_id"],
                    state=state,
                    exit_code=result.get("exit_code"),
                    terminal=bool(result.get("terminal")),
                    catalog_dir=catalog_dir,
                )
            except catalog.SimulationCatalogError as exc:
                errors.append(
                    {
                        "record_id": simulation_id,
                        "run_id": run["run_id"],
                        "error": str(exc),
                    }
                )
                continue

            updated.append(
                {
                    "record_id": simulation_id,
                    "run_id": run["run_id"],
                    "job_id": run["job_id"],
                    "machine": run["machine"],
                    "run_state": state,
                    "exit_code": result.get("exit_code"),
                    "status_before": before,
                    "status_after": record.get("status"),
                }
            )
            before = record.get("status")

    return {
        "polled": plan["counts"]["runs"],
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
        "counts": {
            "simulations": plan["counts"]["simulations"],
            "polled": plan["counts"]["runs"],
            "updated": len(updated),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
        "writes": True,
    }
