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

Against the machines it polls the scheduler, reads a bounded slice of a
run's stdout once to capture the XGC build banner, and never submits or
cancels. The one remote write is opt-in per machine (``campaign.auto``): a
simulation that turns ``completed`` gets its HPC-Campaign archive started on
the login node, and later refreshes collect the result (``tools/campaign.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from tools import campaign
from tools import catalog_sync
from tools import hpc_runner
from tools import machines as machine_config
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


def _bulk_states(
    machine: str, job_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Ask one machine about many jobs in as few remote calls as possible.

    Returns job id -> the same shape ``hpc_runner.status`` produces, or None
    when this scheduler cannot answer in bulk and the caller should fall back
    to one query per job.

    Why this exists: a remote call costs seconds of login-shell startup no
    matter how trivial the command, so the cost of polling is dominated by the
    number of round trips, not by the number of jobs. Two calls per machine
    replaces two per job.
    """
    cfg = machine_config.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)
    if not scheduler.supports_bulk_status:
        return None

    host = ssh.host_for(cfg)
    results: dict[str, dict[str, Any]] = {}

    queued = ssh.run(host, scheduler.bulk_status_argv(job_ids))
    live = scheduler.parse_bulk_status(queued.stdout) if queued.ok else {}
    for job_id, state in live.items():
        results[job_id] = {
            "machine": machine,
            "job_id": job_id,
            "state": state,
            "terminal": state in sched.TERMINAL_STATES,
            "source": scheduler.type_name,
        }

    # Anything the queue no longer lists has finished; accounting knows how.
    missing = [job_id for job_id in job_ids if job_id not in results]
    if missing:
        acct = ssh.run(host, scheduler.bulk_accounting_argv(missing))
        if acct.ok:
            for job_id, (state, exit_code) in scheduler.parse_bulk_accounting(
                acct.stdout
            ).items():
                results[job_id] = {
                    "machine": machine,
                    "job_id": job_id,
                    "state": state,
                    "exit_code": exit_code,
                    "terminal": state in sched.TERMINAL_STATES,
                    "source": "sacct",
                }

    # Jobs neither the queue nor accounting knows are reported as unknown, so
    # the caller leaves them alone rather than overwriting a real state.
    for job_id in job_ids:
        results.setdefault(
            job_id,
            {
                "machine": machine,
                "job_id": job_id,
                "state": sched.UNKNOWN,
                "terminal": False,
                "source": scheduler.type_name,
            },
        )
    return results


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


@catalog_sync.synced(lambda result, **k: "refresh status" + (f" {k['record_id']}" if k.get("record_id") else "") + (f" on {k['machine']}" if k.get("machine") else ""))
def refresh(
    *,
    catalog_dir: Path | None = None,
    machine: str | None = None,
    record_id: str | None = None,
    status_fn: Callable[[str, str], dict[str, Any]] | None = None,
    campaign_hooks: bool | None = None,
) -> dict[str, Any]:
    """Poll every in-flight run and write the scheduler's answers back.

    Unlike the write operations in this toolchain, this needs no confirmation:
    it only records what the scheduler already decided. There is nothing for a
    human to approve, and requiring approval would defeat the purpose of a
    command meant to be run without thinking about it.

    A machine that is unreachable is reported per-run and does not stop the
    rest, so one expired credential cannot block the whole catalog.

    ``campaign_hooks`` (default: on unless ``status_fn`` is given, i.e. off
    for tests) starts campaign archives for newly completed simulations and
    follows up on earlier builds. Their results go to ``campaigns`` and never
    count as refresh errors.
    """
    if campaign_hooks is None:
        campaign_hooks = status_fn is None
    plan = plan_refresh(catalog_dir=catalog_dir, machine=machine, record_id=record_id)
    query = status_fn or hpc_runner.status

    updated: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # Collect every machine's answers first, in as few remote calls as the
    # scheduler allows, then apply them. An explicit status_fn (tests, or a
    # caller wanting per-job semantics) disables bulk querying.
    bulk: dict[str, dict[str, dict[str, Any]]] = {}
    bulk_errors: dict[str, str] = {}
    if status_fn is None:
        by_machine: dict[str, list[str]] = {}
        for target in plan["targets"]:
            for run in target["runs"]:
                by_machine.setdefault(str(run["machine"]), []).append(str(run["job_id"]))
        for machine_name, job_ids in by_machine.items():
            unique = sorted(set(job_ids))
            try:
                answers = _bulk_states(machine_name, unique)
            except (ssh.SSHError, sched.SchedulerError, KeyError) as exc:
                # Record it once per machine; the per-run loop reports it for
                # each affected run without re-attempting the connection.
                bulk_errors[machine_name] = str(exc)
                continue
            if answers is not None:
                bulk[machine_name] = answers

    for target in plan["targets"]:
        simulation_id = target["record_id"]
        before = target["status"]

        for run in target["runs"]:
            run_machine = str(run["machine"])
            job_id = str(run["job_id"])

            if run_machine in bulk_errors:
                errors.append(
                    {
                        "record_id": simulation_id,
                        "run_id": run["run_id"],
                        "machine": run_machine,
                        "job_id": job_id,
                        "error": bulk_errors[run_machine],
                    }
                )
                continue

            cached = bulk.get(run_machine, {}).get(job_id)
            if cached is not None:
                result = cached
            else:
                try:
                    result = query(run_machine, job_id)
                except (ssh.SSHError, sched.SchedulerError, KeyError) as exc:
                    errors.append(
                        {
                            "record_id": simulation_id,
                            "run_id": run["run_id"],
                            "machine": run_machine,
                            "job_id": job_id,
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
            build_capture = hpc_runner.maybe_capture_build(
                run_machine, simulation_id, run["run_id"], state
            )
            dryrun_capture = hpc_runner.maybe_capture_parallel_dryrun(
                run_machine, simulation_id, run["run_id"]
            )

            if state == run.get("state"):
                entry = {
                    "record_id": simulation_id,
                    "run_id": run["run_id"],
                    "state": state,
                }
                if build_capture is not None:
                    entry["build_capture"] = build_capture
                if dryrun_capture is not None:
                    entry["dryrun_parallel"] = dryrun_capture
                unchanged.append(entry)
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

            entry = {
                "record_id": simulation_id,
                "run_id": run["run_id"],
                "job_id": run["job_id"],
                "machine": run["machine"],
                "run_state": state,
                "exit_code": result.get("exit_code"),
                "status_before": before,
                "status_after": record.get("status"),
            }
            if build_capture is not None:
                entry["build_capture"] = build_capture
            if dryrun_capture is not None:
                entry["dryrun_parallel"] = dryrun_capture
            updated.append(entry)
            before = record.get("status")

    campaigns: list[dict[str, Any]] = []
    if campaign_hooks:
        started: set[str] = set()
        for item in updated:
            if item["status_after"] == "completed" and item["status_before"] != "completed":
                outcome = campaign.on_completion(item["record_id"], catalog_dir=catalog_dir)
                if outcome is not None:
                    campaigns.append(outcome)
                    started.add(item["record_id"])
        campaigns.extend(
            campaign.follow_up(
                catalog_dir=catalog_dir,
                machine=machine,
                record_id=record_id,
                skip=started,
            )
        )

    return {
        "polled": plan["counts"]["runs"],
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
        "campaigns": campaigns,
        "counts": {
            "simulations": plan["counts"]["simulations"],
            "polled": plan["counts"]["runs"],
            "updated": len(updated),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
        "writes": True,
    }
