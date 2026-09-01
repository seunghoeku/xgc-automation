"""Unattended queue watch: poll a machine's scheduler, report only changes.

``refresh`` polls what the catalog believes is in flight. That leaves a real
gap: jobs submitted by hand, outside this toolchain, have no catalog record and
so are invisible to it. This walks the other way -- it asks the scheduler what
the user currently has queued, compares against the previous snapshot, and says
something only when the answer changed.

It is built to run from a local timer (launchd/cron) rather than from a chat
session. Polling a queue needs no judgement, and a model re-reading its whole
context every two hours to run one ``squeue`` is a poor trade; this costs
nothing to repeat.

Read-only against the machine: it runs the scheduler's queue and accounting
queries and nothing else. It never submits, cancels, or writes remote files.

Authentication stays the human's job (AGENTS.md rule 3). When the NERSC
certificate has expired the watcher reports that and exits nonzero rather than
attempting anything interactive -- unattended runs stay blind until the user
refreshes sshproxy.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tools import machines as machine_config
from tools import scheduler as sched
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".cache" / "queue-watch"

# How far ahead to warn about an expiring credential. Two poll intervals of
# lead time is enough for the user to run sshproxy before the watch goes blind.
CERTIFICATE_WARNING_WINDOW = timedelta(hours=4)

# Exit codes; launchd only logs them, but a human running this by hand can tell
# "nothing to report" from "could not look".
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
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


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
    jobs = adapter.parse_user_queue(result.stdout)
    return {job["job_id"]: job for job in jobs}


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
        result = ssh.run(target, adapter.accounting_argv(job_id))
        if result.ok:
            finals[job_id] = adapter.parse_accounting(result.stdout)
    return finals


def _describe(job: dict[str, Any]) -> str:
    bits = [f"{job.get('nodes', '?')} node(s)"]
    if job.get("state") == sched.RUNNING and job.get("elapsed"):
        bits.append(f"elapsed {job['elapsed']}")
    elif job.get("reason"):
        bits.append(str(job["reason"]))
    if job.get("time_limit"):
        bits.append(f"limit {job['time_limit']}")
    return ", ".join(bits)


def diff_queues(
    machine: str,
    previous: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    """Human-readable lines for everything that changed since the last poll."""
    changes: list[str] = []

    for job_id, job in sorted(current.items()):
        before = previous.get(job_id)
        if before is None:
            changes.append(
                f"NEW      {job_id} {job.get('name', '?')} "
                f"[{job.get('state', '?')}] {_describe(job)}"
            )
        elif before.get("state") != job.get("state"):
            changes.append(
                f"CHANGED  {job_id} {job.get('name', '?')} "
                f"{before.get('state', '?')} -> {job.get('state', '?')} "
                f"({_describe(job)})"
            )

    departed = sorted(set(previous) - set(current))
    finals = _final_states(machine, departed) if departed else {}
    for job_id in departed:
        job = previous[job_id]
        state, exit_code = finals.get(job_id, (sched.UNKNOWN, None))
        suffix = f", exit {exit_code}" if exit_code else ""
        changes.append(
            f"FINISHED {job_id} {job.get('name', '?')} [{state}{suffix}] "
            f"in {job.get('work_dir', '?')}"
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
) -> dict[str, Any]:
    """Poll once, diff against the stored snapshot, and record the new one."""
    path = state_file or state_path(machine)
    previous = load_state(path).get("jobs", {})
    current = poll(machine)
    changes = diff_queues(machine, previous, current)

    note = _certificate_note(machine)
    if note:
        changes.append(f"NOTE     {note}")

    save_state(
        path,
        {"machine": machine, "polled_at": _now().isoformat(), "jobs": current},
    )

    if changes and send_notification:
        notify(f"XGC queue: {machine}", "; ".join(changes)[:500])

    return {
        "machine": machine,
        "polled_at": _now().isoformat(),
        "queued": len(current),
        "changes": changes,
        "jobs": current,
        "reported": bool(changes) or always_report,
    }


def _print_report(payload: dict[str, Any], *, always_report: bool) -> None:
    stamp = payload["polled_at"]
    machine = payload["machine"]
    if payload["changes"]:
        print(f"[{stamp}] {machine}: {payload['queued']} job(s) queued")
        for line in payload["changes"]:
            print(f"  {line}")
        for job_id, job in sorted(payload["jobs"].items()):
            print(
                f"  now      {job_id} {job.get('name', '?')} "
                f"[{job.get('state', '?')}] {_describe(job)}"
            )
    elif always_report:
        print(f"[{stamp}] {machine}: {payload['queued']} job(s) queued")
        for job_id, job in sorted(payload["jobs"].items()):
            print(
                f"  {job_id} {job.get('name', '?')} "
                f"[{job.get('state', '?')}] {_describe(job)}"
            )
    else:
        print(f"[{stamp}] {machine}: no change ({payload['queued']} queued)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xgc-queue-watch",
        description="Poll a machine's scheduler queue and report only changes.",
    )
    parser.add_argument("--machine", default="perlmutter")
    parser.add_argument(
        "--state-file",
        help="where the previous snapshot lives (default: .cache/queue-watch/)",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="log changes without a desktop notification",
    )
    parser.add_argument(
        "--always-report",
        action="store_true",
        help="print the full queue even when nothing changed",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        payload = watch(
            args.machine,
            state_file=Path(os.path.expanduser(args.state_file))
            if args.state_file
            else None,
            send_notification=not args.no_notify,
            always_report=args.always_report,
        )
    except ssh.SSHError as error:
        message = str(error)
        print(f"[{_now().isoformat()}] {args.machine}: unreachable: {message}")
        if not args.no_notify:
            notify(f"XGC queue: {args.machine}", f"Cannot poll: {message}"[:500])
        return EXIT_UNREACHABLE
    except (QueueWatchError, sched.SchedulerError, KeyError) as error:
        print(f"[{_now().isoformat()}] {args.machine}: error: {error}")
        return EXIT_ERROR

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_report(payload, always_report=args.always_report)
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
