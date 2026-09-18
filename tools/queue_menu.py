"""SwiftBar text presentation of local queue snapshots; never connects to HPC."""

from datetime import datetime
from typing import Any

from . import scheduler as sched

ICONS = {
    sched.RUNNING: "🟢", sched.PENDING: "🟡", sched.COMPLETED: "✅",
    sched.FAILED: "🔴", sched.CANCELLED: "⚪", sched.TIMEOUT: "🟠",
    sched.UNKNOWN: "❔",
}

LABELS = {
    state: f"{ICONS[state]} {label}"
    for state, label in {
        sched.RUNNING: "Running", sched.PENDING: "Pending",
        sched.COMPLETED: "Completed", sched.FAILED: "Failed",
        sched.CANCELLED: "Cancelled", sched.TIMEOUT: "Time limit reached",
        sched.UNKNOWN: "Outcome unconfirmed",
    }.items()
}


def clean(value: Any) -> str:
    # Scheduler-controlled text must not inject SwiftBar parameters/actions.
    text = " ".join(str(value).split()).replace("|", "｜")
    return "".join(c for c in text if c.isprintable())


def age(stamp: str | None, now: datetime) -> str:
    try:
        when = datetime.fromisoformat(stamp or "")
        seconds = max(0, (now - when).total_seconds())
    except (ValueError, TypeError):
        return "Not checked yet"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def stale(snapshot: dict[str, Any], now: datetime, *, after_seconds: int = 1200) -> bool:
    try:
        stamp = datetime.fromisoformat(snapshot.get("polled_at", ""))
        return (now - stamp).total_seconds() > after_seconds
    except (ValueError, TypeError):
        return True


def estimated_start_label(job: dict[str, Any]) -> str:
    """Display an absolute estimate in the Mac's local zone, never guess one."""
    try:
        when = datetime.fromisoformat(job.get("estimated_start") or "")
        if when.tzinfo is None:
            return "unavailable"
        local = when.astimezone()
        label = local.strftime("%Y-%m-%d %H:%M %Z")
        if when < datetime.now().astimezone():
            label += " (estimate passed)"
        return label
    except (ValueError, TypeError):
        return "unavailable"


def machine_state_icon(snapshot: dict[str, Any]) -> str:
    """Summarize jobs with the menu's explicit state priority."""
    jobs = [*snapshot.get("jobs", {}).values(),
            *snapshot.get("recent_finished", [])]
    for state in (sched.RUNNING, sched.PENDING, sched.COMPLETED, sched.FAILED):
        if any(job.get("state") == state for job in jobs):
            return ICONS[state]
    return ""


def job_lines(job: dict[str, Any], *, finished: bool = False) -> list[str]:
    state = LABELS.get(job.get("state"), "❔ Unknown")
    ident = job.get("display_id") or job.get("job_id", "?")
    lines = [f"--{state} · {clean(job.get('name', '?'))} · #{clean(ident)} · "
             f"{clean(job.get('nodes') or '?')} nodes · "
             f"requested {clean(job.get('time_limit') or '?')}"]
    if not finished and job.get("state") == sched.PENDING:
        lines[0] += f" · Est. start: {estimated_start_label(job)}"
    if finished:
        lines.append(f"----Left queue (observed): {clean(job.get('left_queue_at', '?'))}")
        if job.get("exit_code") is not None:
            lines.append(f"----Exit: {clean(job['exit_code'])}")
    else:
        if job.get("elapsed"):
            lines.append(f"----Elapsed {clean(job['elapsed'])}")
        if job.get("reason"):
            lines.append(f"----{clean(job['reason'])}")
    return lines


def render(snapshots: list[dict[str, Any]], *, history_limit: int = 5,
           now: datetime | None = None, stale_after_seconds: int = 1200) -> str:
    now = now or datetime.now().astimezone()
    running = pending = warnings = 0
    for snapshot in snapshots:
        if (snapshot.get("connection", "ok") != "ok"
                or stale(snapshot, now, after_seconds=stale_after_seconds)):
            warnings += 1
            continue
        for job in snapshot.get("jobs", {}).values():
            running += job.get("state") == sched.RUNNING
            pending += job.get("state") == sched.PENDING
    title = f"HPC · Running {running} · Pending {pending}"
    if warnings:
        title += f" · ⚠ {warnings}"
    lines = [title, "---"]
    for snapshot in snapshots:
        unavailable = snapshot.get("connection", "ok") != "ok"
        old = stale(snapshot, now, after_seconds=stale_after_seconds)
        connection_icon = "⚠ " if unavailable or old else ""
        state_icon = machine_state_icon(snapshot)
        state_prefix = f"{state_icon} " if state_icon else ""
        checked = (f"{age(snapshot['polled_at'], now)}"
                   if snapshot.get("polled_at") else "Not checked yet")
        lines.append(
            f"{connection_icon}{state_prefix}{clean(snapshot['machine'])} · {checked}"
        )
        if unavailable:
            lines.append(f"--Connection unavailable: {clean(snapshot.get('detail', '?'))}")
        elif old:
            lines.append("--Update overdue · last known state")
        if snapshot.get("note"):
            lines.append(f"--{clean(snapshot['note'])}")
        jobs = sorted(snapshot.get("jobs", {}).values(),
                      key=lambda j: (j.get("state") != sched.RUNNING, str(j.get("job_id"))))
        lines.append("--Last known queue" if unavailable or old else "--Current queue")
        if not jobs:
            lines.append("--No jobs" if snapshot.get("polled_at") else "--No cached data")
        for job in jobs:
            lines.extend(job_lines(job))
        recent = snapshot.get("recent_finished", [])[:history_limit]
        lines.append(f"--Recent departures / unconfirmed ({len(recent)})")
        for job in recent:
            lines.extend(job_lines(job, finished=True))
    lines.extend(["---", "Polling: separate timer · menu refresh: 30s",
                  "Refresh cached view | refresh=true"])
    return "\n".join(lines)
