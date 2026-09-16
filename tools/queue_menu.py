"""SwiftBar text presentation of local queue snapshots; never connects to HPC."""

from datetime import datetime
from typing import Any

from tools import scheduler as sched

LABELS = {
    sched.RUNNING: "🟢 실행 중", sched.PENDING: "🟡 대기 중",
    sched.COMPLETED: "✅ 잡 완료", sched.FAILED: "🔴 실패",
    sched.CANCELLED: "⚪ 취소", sched.TIMEOUT: "🟠 시간 제한 종료",
    sched.UNKNOWN: "❔ 종료 상태 확인 중",
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
        return "확인 기록 없음"
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 86400:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def stale(snapshot: dict[str, Any], now: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(snapshot.get("polled_at", ""))
        return (now - stamp).total_seconds() > 1200
    except (ValueError, TypeError):
        return True


def job_lines(job: dict[str, Any], *, finished: bool = False) -> list[str]:
    state = LABELS.get(job.get("state"), "❔ 상태 미확인")
    ident = job.get("display_id") or job.get("job_id", "?")
    lines = [f"--{state} · {clean(job.get('name', '?'))} · #{clean(ident)} · "
             f"{clean(job.get('nodes') or '?')} nodes · "
             f"요청 {clean(job.get('time_limit') or '?')}"]
    if finished:
        lines.append(f"----큐 이탈 확인: {clean(job.get('left_queue_at', '?'))}")
        if job.get("exit_code") is not None:
            lines.append(f"----Exit: {clean(job['exit_code'])}")
    else:
        if job.get("elapsed"):
            lines.append(f"----경과 {clean(job['elapsed'])}")
        if job.get("reason"):
            lines.append(f"----{clean(job['reason'])}")
    return lines


def render(snapshots: list[dict[str, Any]], *, history_limit: int = 5,
           now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    running = pending = warnings = 0
    for snapshot in snapshots:
        if snapshot.get("connection", "ok") != "ok" or stale(snapshot, now):
            warnings += 1
            continue
        for job in snapshot.get("jobs", {}).values():
            running += job.get("state") == sched.RUNNING
            pending += job.get("state") == sched.PENDING
    title = f"HPC · 실행 {running} · 대기 {pending}"
    if warnings:
        title += f" · ⚠ {warnings}"
    lines = [title, "---"]
    for snapshot in snapshots:
        unavailable = snapshot.get("connection", "ok") != "ok"
        old = stale(snapshot, now)
        status = "⚠ " if unavailable or old else ""
        checked = (f"{age(snapshot['polled_at'], now)} 확인"
                   if snapshot.get("polled_at") else "확인 기록 없음")
        lines.append(f"{status}{clean(snapshot['machine'])} · {checked}")
        if unavailable:
            lines.append(f"--연결 확인 필요: {clean(snapshot.get('detail', '?'))}")
        elif old:
            lines.append("--갱신 지연 · 마지막 확인 상태")
        if snapshot.get("note"):
            lines.append(f"--{clean(snapshot['note'])}")
        jobs = sorted(snapshot.get("jobs", {}).values(),
                      key=lambda j: (j.get("state") != sched.RUNNING, str(j.get("job_id"))))
        lines.append("--마지막 확인 큐" if unavailable or old else "--현재 큐")
        if not jobs:
            lines.append("--잡 없음" if snapshot.get("polled_at") else "--조회 기록 없음")
        for job in jobs:
            lines.extend(job_lines(job))
        recent = snapshot.get("recent_finished", [])[:history_limit]
        lines.append(f"--최근 종료 / 확인 중 ({len(recent)})")
        for job in recent:
            lines.extend(job_lines(job, finished=True))
    lines.extend(["---", "상태 조회: 별도 타이머 · 화면 갱신: 30초",
                  "저장된 화면 새로고침 | refresh=true"])
    return "\n".join(lines)
