"""Unattended queue watch: poll a machine's scheduler, report only changes.

``refresh`` polls what the catalog believes is in flight. That leaves a real
gap: jobs submitted by hand, outside this toolchain, have no catalog record and
so are invisible to it. This walks the other way -- it asks the scheduler what
the user currently has queued, compares against the previous snapshot, and says
something only when the answer changed.

It is built to run from a local timer (launchd/cron) rather than from a chat
session. Polling a queue needs no judgement, and a model re-reading its whole
context every ten minutes to run one ``squeue`` is a poor trade; this costs
nothing to repeat, which is what makes a short cadence affordable at all.

Every configured machine is polled, not one named machine. Which of them can
actually be reached varies through the day -- a NERSC certificate lasts 24 h,
and an MFA host is only reachable while the user's own ControlMaster session
is alive -- so reachability is a per-poll fact rather than configuration.
``probe`` establishes it without authenticating, and an unreachable machine is
reported as skipped and left alone. Being blind to one machine is normal
operation here, not an error; being blind to all of them is what deserves a
nonzero exit.

Read-only against the machine: it runs the scheduler's queue and accounting
queries and nothing else. It never submits, cancels, or writes remote files.

Authentication stays the human's job (AGENTS.md rule 3). The watcher never
attempts anything interactive; it reports what the user must refresh (an
sshproxy certificate, an MFA session) and polls whatever else it can.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import machines as machine_config
from . import scheduler as sched
from . import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".cache" / "queue-watch"
HISTORY_LIMIT = 5

# How far ahead to warn about an expiring credential. Enough lead time for the
# user to run sshproxy before the watch goes blind, independent of how often
# the timer fires.
CERTIFICATE_WARNING_WINDOW = timedelta(hours=4)

# How long one log file covers before it is moved aside. Exactly two
# generations exist -- the live file and one archive -- so history spans one to
# two weeks depending on where in the cycle you look.
LOG_ROTATION_INTERVAL = timedelta(days=7)

# Suffix of the single archive generation.
LOG_ARCHIVE_SUFFIX = ".1"

# Exit codes; launchd only logs them, but a human running this by hand can tell
# "nothing to report" from "could not look". With several machines in play,
# UNREACHABLE is reserved for being blind to every one of them: a laptop with
# no live sessions at all is a different situation from one machine of three
# needing a credential refresh.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNREACHABLE = 2


class QueueWatchError(RuntimeError):
    """The queue could not be inspected."""


def _now() -> datetime:
    return datetime.now().astimezone()


def state_path(machine: str) -> Path:
    return STATE_DIR / f"{machine}.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A missing or corrupt snapshot means "first run": report the current
        # queue as-is rather than failing the poll.
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The menu bar can read while the timer writes. Publish a complete snapshot.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(state, out, indent=2, sort_keys=True)
            out.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _user_for(machine: str, cfg: dict[str, Any]) -> str:
    user = cfg.get("user")
    if not user or machine_config.is_placeholder(user):
        raise QueueWatchError(
            f"no username configured for {machine!r}; set it in "
            "machines.local.yaml (see machines.local.yaml.example)"
        )
    return str(user)


def poll(machine: str) -> dict[str, Any]:
    """Return the machine's current queue for this user, keyed by job id."""
    cfg = machine_config.get_machine(machine)
    adapter = sched.get_scheduler(cfg)
    user = _user_for(machine, cfg)
    target = ssh.host_for(cfg)

    result = ssh.run(target, adapter.user_queue_argv(user)).check()
    jobs = {job["job_id"]: job for job in adapter.parse_user_queue(result.stdout)}

    # Some schedulers can only name the user's jobs in one call and describe
    # them in a second (see Scheduler.user_queue_detail_argv). The failure is
    # checked rather than tolerated: half-filled jobs would be reported as
    # state "unknown", which reads like a finished job rather than a lookup
    # that did not happen.
    detail_argv = adapter.user_queue_detail_argv(sorted(jobs)) if jobs else None
    if detail_argv:
        detail = adapter.parse_user_queue_detail(
            ssh.run(target, detail_argv).check().stdout
        )
        for job_id, fields in detail.items():
            if job_id in jobs:
                jobs[job_id].update(fields)

    return jobs


def _final_states(machine: str, job_ids: list[str]) -> dict[str, tuple[str, str | None]]:
    """Ask accounting how the jobs that left the queue actually ended."""
    if not job_ids:
        return {}
    cfg = machine_config.get_machine(machine)
    adapter = sched.get_scheduler(cfg)
    target = ssh.host_for(cfg)

    if getattr(adapter, "supports_bulk_status", False):
        result = ssh.run(target, adapter.bulk_accounting_argv(job_ids))
        if result.ok:
            return adapter.parse_bulk_accounting(result.stdout)
        return {}

    finals: dict[str, tuple[str, str | None]] = {}
    for job_id in job_ids:
        try:
            result = ssh.run(target, adapter.accounting_argv(job_id))
        except NotImplementedError:
            # An adapter that cannot look up a finished job leaves the outcome
            # unknown, which diff_queues already reports honestly.
            return {}
        if result.ok:
            finals[job_id] = adapter.parse_accounting(result.stdout)
    return finals


def _ident(job: dict[str, Any]) -> str:
    """The job id as a human reads it.

    Slurm ids are already short. A PBS id carries the whole server FQDN, so
    the adapter supplies a shortened form; preferring it here keeps the
    reporting code free of any scheduler-specific branch (AGENTS.md).
    """
    return str(job.get("display_id") or job.get("job_id") or "?")


def _describe(job: dict[str, Any]) -> str:
    bits = [f"{job.get('nodes', '?')} node(s)"]
    if job.get("state") == sched.RUNNING and job.get("elapsed"):
        bits.append(f"elapsed {job['elapsed']}")
    elif job.get("reason"):
        bits.append(str(job["reason"]))
    if job.get("time_limit"):
        bits.append(f"limit {job['time_limit']}")
    if job.get("state") == sched.PENDING:
        bits.append(f"est. start {job.get('estimated_start') or 'unavailable'}")
    return ", ".join(bits)


def diff_queues(
    machine: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    finals: dict[str, tuple[str, str | None]] | None = None,
) -> list[str]:
    """Human-readable lines for everything that changed since the last poll."""
    changes: list[str] = []

    for job_id, job in sorted(current.items()):
        before = previous.get(job_id)
        if before is None:
            changes.append(
                f"NEW      {_ident(job)} {job.get('name', '?')} "
                f"[{job.get('state', '?')}] {_describe(job)}"
            )
        elif before.get("state") != job.get("state"):
            changes.append(
                f"CHANGED  {_ident(job)} {job.get('name', '?')} "
                f"{before.get('state', '?')} -> {job.get('state', '?')} "
                f"({_describe(job)})"
            )

    departed = sorted(set(previous) - set(current))
    if finals is None:
        finals = _final_states(machine, departed) if departed else {}
    for job_id in departed:
        job = previous[job_id]
        state, exit_code = finals.get(job_id, (sched.UNKNOWN, None))
        suffix = f", exit {exit_code}" if exit_code else ""
        changes.append(
            f"FINISHED {_ident(job)} {job.get('name', '?')} [{state}{suffix}] "
            f"{_describe(job)}, in {job.get('work_dir', '?')}"
        )

    return changes


def _certificate_note(machine: str) -> str | None:
    """Warn before an sshproxy certificate expires, not only after."""
    cfg = machine_config.get_machine(machine)
    if (cfg.get("auth") or {}).get("method") != "sshproxy":
        return None
    try:
        status = ssh.certificate_status(ssh.host_for(cfg).host)
    except ssh.SSHError:
        return None
    if not status or not status.get("valid"):
        return None
    valid_to = status.get("valid_to")
    if not valid_to:
        return None
    try:
        expiry = datetime.fromisoformat(str(valid_to))
    except ValueError:
        return None
    remaining = expiry - datetime.now()
    if remaining <= CERTIFICATE_WARNING_WINDOW:
        hours = max(remaining.total_seconds(), 0) / 3600
        return (
            f"SSH certificate expires in {hours:.1f} h ({valid_to}); "
            "refresh it with your NERSC sshproxy workflow or the watch goes blind"
        )
    return None


def rotate_log(
    path: Path,
    *,
    interval: timedelta = LOG_ROTATION_INTERVAL,
    now: datetime | None = None,
    marker: Path | None = None,
) -> bool:
    """Move the log aside once per ``interval``, keeping exactly one archive.

    ``os.replace`` overwrites the previous archive atomically, so the file
    count stays at two and the older generation is discarded without a
    separate delete.

    Rotating by rename works under launchd, and alternating between two log
    names would not: ``StandardOutPath`` is fixed in the plist, so a scheme
    that wrote to a different name each week would need the plist rewritten
    and reloaded from inside a poll. Moving the content out from under one
    fixed name needs neither -- launchd opens that path afresh for every poll,
    so the next one creates a new file where the old one was.

    The caller rotates *after* reporting, because a process's stdout is bound
    to the inode rather than the name: whatever this poll printed follows the
    file into the archive and closes out the week it belongs to, and the
    following poll starts the new file.

    Returns:
        Whether a log file was actually moved.
    """
    moment = now or _now()
    marker_path = marker or STATE_DIR / f"{path.name}.rotated-at"

    # The marker records when the live file's week began. The log's own mtime
    # cannot answer that -- it moves with every poll -- and reading the first
    # line back would put the whole scheme at the mercy of the report format.
    due = True
    try:
        started = datetime.fromisoformat(
            marker_path.read_text(encoding="utf-8").strip()
        )
    except (OSError, ValueError):
        # No readable marker: the live file's age is unknown, so close it out
        # and start a dated one. Nothing is lost; it becomes the archive.
        pass
    else:
        if started.tzinfo is None:
            started = started.astimezone()
        due = moment - started >= interval

    if not due:
        return False

    rotated = False
    if path.exists():
        os.replace(path, path.with_name(path.name + LOG_ARCHIVE_SUFFIX))
        rotated = True

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(moment.isoformat(), encoding="utf-8")
    return rotated


def probe(machine: str) -> dict[str, Any]:
    """Whether this machine can be polled right now, without authenticating.

    Reachability is checked before the poll rather than inferred from its
    failure, because the two are not the same shape: an expired certificate or
    a dead ControlMaster is a credential the user must refresh, and saying so
    is more useful than a timeout. Every check here is either local or a
    bounded probe, and none of them can prompt (``ssh.py`` forces BatchMode).

    Returns:
        ``{"machine", "pollable", "detail"}``. Never raises.
    """
    try:
        cfg = machine_config.get_machine(machine)
    except KeyError as error:
        return {"machine": machine, "pollable": False, "detail": str(error)}

    try:
        sched.get_scheduler(cfg)
        _user_for(machine, cfg)
        target = ssh.host_for(cfg)
    except (sched.SchedulerError, QueueWatchError, ssh.SSHError) as error:
        return {"machine": machine, "pollable": False, "detail": str(error)}

    try:
        # Validates the sshproxy certificate locally, or checks that an MFA
        # host has a live ControlMaster socket. Neither authenticates.
        ssh.connection_options(target)
    except ssh.SSHError as error:
        return {"machine": machine, "pollable": False, "detail": str(error)}

    if target.auth_method not in {"sshproxy", "key", ssh.LOCAL_METHOD}:
        # A live master can still be unable to open a command channel: the
        # socket check only inspects the local process. This bounded probe is
        # the difference between "skipped, session is stale" and a poll that
        # hangs until the SSH timeout.
        usable, detail = ssh.command_channel_usable(target.host)
        if not usable:
            return {
                "machine": machine,
                "pollable": False,
                "detail": (
                    f"ssh session for {target.host!r} is live but cannot open a "
                    f"command channel ({detail}); reconnect it"
                ),
            }

    return {"machine": machine, "pollable": True, "detail": f"{target.host} ready"}


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification; never fails the poll."""
    try:
        if platform.system() == "Darwin" and shutil.which("osascript"):
            body = message.replace("\\", "\\\\").replace('"', '\\"')
            heading = title.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{body}" with title "{heading}"',
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        return


def watch(
    machine: str,
    *,
    state_file: Path | None = None,
    send_notification: bool = True,
    always_report: bool = False,
    history_limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    """Poll once, diff against the stored snapshot, and record the new one."""
    path = state_file or state_path(machine)
    if history_limit < 1:
        raise ValueError("history_limit must be positive")
    saved = load_state(path)
    previous = saved.get("jobs", {})
    current = poll(machine)
    moment = _now().isoformat()
    history = {j["job_id"]: j for j in saved.get("recent_finished", [])
               if j["job_id"] not in current}
    departed = sorted(set(previous) - set(current))
    unresolved = [key for key, j in history.items()
                  if j.get("state") not in sched.TERMINAL_STATES]
    query_ids = sorted(set(departed + unresolved))
    # Accounting may lag behind the queue. Preserve and retry unknown outcomes.
    try:
        finals = _final_states(machine, query_ids) if query_ids else {}
    except (ssh.SSHError, sched.SchedulerError):
        finals = {}
    changes = diff_queues(machine, previous, current, finals=finals)
    for key in departed:
        history[key] = {**previous[key], "left_queue_at": moment,
                        "state": sched.UNKNOWN, "exit_code": None}
        history[key].pop("raw_state", None)
    for key in query_ids:
        state, code = finals.get(key, (sched.UNKNOWN, None))
        if state in sched.TERMINAL_STATES:
            history[key].update(state=state, exit_code=code)
            if key not in departed:
                changes.append(f"RESOLVED {_ident(history[key])} "
                               f"{history[key].get('name', '?')} [{state}]")
    recent = sorted(history.values(), key=lambda j: j["left_queue_at"], reverse=True)
    recent = recent[:history_limit]

    note = _certificate_note(machine)
    if note:
        changes.append(f"NOTE     {note}")

    save_state(
        path,
        {"machine": machine, "polled_at": moment, "jobs": current,
         "recent_finished": recent, "checked_at": moment, "connection": "ok",
         "note": note},
    )

    if changes and send_notification:
        notify(f"XGC queue: {machine}", "; ".join(changes)[:500])

    return {
        "machine": machine,
        "polled_at": _now().isoformat(),
        "queued": len(current),
        "changes": changes,
        "jobs": current,
        "recent_finished": recent,
        "reported": bool(changes) or always_report,
    }


def watch_many(
    machines: Sequence[str],
    *,
    state_files: dict[str, Path] | None = None,
    send_notification: bool = True,
    always_report: bool = True,
    history_limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    """Poll every machine that can be reached, skip the rest, notify once.

    One machine being unreachable must not stop the others: the common case on
    a laptop is exactly that, with a lapsed NERSC certificate next to a live
    MFA session. Notification is aggregated deliberately -- one banner listing
    what changed everywhere beats one banner per machine.
    """
    results: list[dict[str, Any]] = []
    changes: list[str] = []

    for machine in machines:
        state_file = (state_files or {}).get(machine)
        path = state_file or state_path(machine)
        status = probe(machine)
        if not status["pollable"]:
            save_state(path, {**load_state(path), "machine": machine,
                             "checked_at": _now().isoformat(),
                             "connection": "skipped", "detail": status["detail"]})
            results.append(
                {"machine": machine, "polled_at": _now().isoformat(),
                 "skipped": status["detail"]}
            )
            continue

        try:
            payload = watch(
                machine,
                state_file=state_file,
                send_notification=False,
                always_report=always_report,
                history_limit=history_limit,
            )
        except (ssh.SSHError, QueueWatchError, sched.SchedulerError, KeyError) as error:
            save_state(path, {**load_state(path), "machine": machine,
                             "checked_at": _now().isoformat(),
                             "connection": "error", "detail": str(error)})
            results.append(
                {"machine": machine, "polled_at": _now().isoformat(),
                 "error": str(error)}
            )
            continue

        results.append(payload)
        changes.extend(f"{machine}: {line}" for line in payload["changes"])

    polled = [r for r in results if "jobs" in r]
    skipped = [r for r in results if "skipped" in r]
    failed = [r for r in results if "error" in r]

    if changes and send_notification:
        label = polled[0]["machine"] if len(polled) == 1 else "queues"
        notify(f"XGC queue: {label}", "; ".join(changes)[:500])

    return {
        "polled_at": _now().isoformat(),
        "machines": results,
        "changes": changes,
        "queued": sum(r["queued"] for r in polled),
        "polled": [r["machine"] for r in polled],
        "skipped": [r["machine"] for r in skipped],
        "failed": [r["machine"] for r in failed],
    }


def exit_code_for(payload: dict[str, Any], requested: Sequence[str]) -> int:
    """Map a multi-machine poll onto an exit code.

    A skipped machine is normal operation, so it does not fail the run. Being
    blind to every requested machine does, and so does an unexpected error,
    because those are the two states a human has to act on.
    """
    if payload["failed"]:
        return EXIT_ERROR
    if requested and not payload["polled"]:
        return EXIT_UNREACHABLE
    return EXIT_OK


def _print_machine(payload: dict[str, Any], *, always_report: bool) -> None:
    """One machine's block: what changed, then the queue as it stands."""
    stamp = payload["polled_at"]
    machine = payload["machine"]

    if "skipped" in payload:
        print(f"[{stamp}] {machine}: skipped -- {payload['skipped']}")
        return
    if "error" in payload:
        print(f"[{stamp}] {machine}: error: {payload['error']}")
        return

    if not payload["changes"] and not always_report:
        print(f"[{stamp}] {machine}: no change ({payload['queued']} queued)")
        return

    print(f"[{stamp}] {machine}: {payload['queued']} job(s) queued")
    for line in payload["changes"]:
        print(f"  {line}")
    for _, job in sorted(payload["jobs"].items()):
        print(
            f"  now      {_ident(job)} {job.get('name', '?')} "
            f"[{job.get('state', '?')}] {_describe(job)}"
        )
    for job in payload.get("recent_finished", []):
        print(f"  recent   {_ident(job)} {job.get('name', '?')} "
              f"[{job['state']}] {job.get('nodes', '?')} node(s), "
              f"limit {job.get('time_limit', '?')}, left queue observed "
              f"{job['left_queue_at']}")


def _print_report(payload: dict[str, Any], *, always_report: bool) -> None:
    """Print every machine's block, then a one-line tally when there are several."""
    for machine_payload in payload["machines"]:
        _print_machine(machine_payload, always_report=always_report)

    if len(payload["machines"]) > 1:
        print(
            f"[{payload['polled_at']}] {len(payload['polled'])} polled, "
            f"{len(payload['skipped'])} skipped, {len(payload['failed'])} failed; "
            f"{payload['queued']} job(s) queued in total"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xgc-queue-watch",
        description=(
            "Poll the scheduler queues of every reachable machine and list "
            "this user's jobs."
        ),
    )
    parser.add_argument(
        "--machine",
        action="append",
        metavar="NAME",
        help=(
            "machine to poll; repeatable. Default: every machine in "
            "machines.yaml, skipping those that cannot be reached."
        ),
    )
    parser.add_argument(
        "--state-file",
        help=(
            "where the previous snapshot lives (default: .cache/queue-watch/). "
            "Requires exactly one --machine."
        ),
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="log changes without a desktop notification",
    )
    parser.add_argument(
        "--changes-only",
        action="store_true",
        help="log only what changed, instead of the full queue every poll",
    )
    parser.add_argument(
        "--rotate-log",
        metavar="PATH",
        help=(
            "rotate this log file once a week, keeping one archive alongside it "
            "as PATH.1 (the timer points it at its own StandardOutPath). "
            "Omitted, nothing is rotated."
        ),
    )
    parser.add_argument(
        "--log-rotation-days",
        type=int,
        default=LOG_ROTATION_INTERVAL.days,
        metavar="N",
        help=(
            "how long one log file covers before it is archived "
            f"(default: {LOG_ROTATION_INTERVAL.days})"
        ),
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable output")
    output.add_argument("--swiftbar", action="store_true",
                        help="render cached state for SwiftBar without SSH or writes")
    parser.add_argument("--history-limit", type=int, default=HISTORY_LIMIT,
                        help="recent departed jobs per machine (default: 5)")
    args = parser.parse_args(argv)

    if args.log_rotation_days < 1:
        parser.error("--log-rotation-days must be at least 1")
    if args.history_limit < 1:
        parser.error("--history-limit must be at least 1")

    machines = args.machine or machine_config.machine_names()
    if args.state_file and len(machines) != 1:
        parser.error("--state-file applies to a single machine; pass one --machine")

    state_files = (
        {machines[0]: Path(os.path.expanduser(args.state_file))}
        if args.state_file
        else None
    )
    if args.swiftbar:
        from .queue_menu import render
        print(render([load_state((state_files or {}).get(m, state_path(m)))
                      | {"machine": m} for m in machines],
                     history_limit=args.history_limit))
        return EXIT_OK
    always_report = not args.changes_only

    payload = watch_many(
        machines,
        state_files=state_files,
        send_notification=not args.no_notify,
        always_report=always_report,
        history_limit=args.history_limit,
    )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_report(payload, always_report=always_report)
    sys.stdout.flush()

    # Last, so this poll's report closes out the week being archived, and never
    # fatal: rotating a log is housekeeping and the poll is the point.
    if args.rotate_log:
        target = Path(os.path.expanduser(args.rotate_log))
        try:
            rotate_log(target, interval=timedelta(days=args.log_rotation_days))
        except OSError as error:
            print(f"[{_now().isoformat()}] could not rotate {target}: {error}")
            sys.stdout.flush()

    return exit_code_for(payload, machines)


if __name__ == "__main__":
    raise SystemExit(main())
