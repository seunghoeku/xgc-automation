"""Job submission, monitoring, and cancellation.

Submission is deliberately staged (AGENTS.md, absolute rules 1 and 4):

    plan = prepare_submission(...)   # renders the script, estimates cost
    # human reviews plan["script"] and plan["resources"]
    submit(..., case_dir=..., confirm=True)
        # 1. xgc --dryrun validates the input on the login node (seconds, free)
        # 2. only if it passes does anything reach the scheduler

`submit` without `confirm=True` returns the plan and submits nothing. Both
gates live here rather than in the MCP layer so they hold no matter who calls.

Every submission writes a provenance record to runs/<run_id>/, including the
dryrun report, so a run's validation state is recoverable later (rule 5).
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tools import dryrun
from tools import machines as m
from tools import scheduler as sched
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
RUNS_DIR = PROJECT_ROOT / "runs"


class SubmissionError(RuntimeError):
    """Submission could not proceed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jinja_env() -> Environment:
    # StrictUndefined: a missing template variable must fail loudly rather than
    # silently rendering an empty #SBATCH directive.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def new_run_id(prefix: str = "run") -> str:
    """Timestamp-based run id. Sortable, unique enough for a single user."""
    return f"{prefix}-{_now().strftime('%Y%m%dT%H%M%SZ')}"


@dataclass
class SubmissionPlan:
    """What would be submitted. Produced for human review before anything runs."""

    machine: str
    run_id: str
    job_name: str
    script: str
    remote_run_dir: str
    remote_script_path: str
    resources: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "run_id": self.run_id,
            "job_name": self.job_name,
            "remote_run_dir": self.remote_run_dir,
            "remote_script_path": self.remote_script_path,
            "resources": self.resources,
            "warnings": self.warnings,
            "script": self.script,
        }


def _walltime_to_hours(walltime: str) -> float:
    """Convert HH:MM:SS or D-HH:MM:SS to hours. Returns 0.0 if unparseable."""
    try:
        days = 0
        if "-" in walltime:
            day_part, walltime = walltime.split("-", 1)
            days = int(day_part)
        parts = [int(p) for p in walltime.split(":")]
        while len(parts) < 3:
            parts.append(0)
        hours, minutes, seconds = parts[:3]
        return days * 24 + hours + minutes / 60 + seconds / 3600
    except (ValueError, AttributeError):
        return 0.0


def prepare_submission(
    machine: str,
    *,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    qos: str | None = None,
    constraint: str | None = None,
    xgc_exe: str | None = None,
    run_id: str | None = None,
    hello_world: bool = False,
) -> SubmissionPlan:
    """Render the job script and estimate cost. Submits nothing.

    Args:
        machine: key in machines.yaml.
        hello_world: ignore the XGC executable and run `srun hostname` instead.
        xgc_exe: override the executable from machines.yaml.

    Unspecified resource arguments fall back to the machine's `defaults`.
    """
    cfg = m.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)  # raises on unconfirmed schedulers
    defaults = cfg.get("defaults", {})
    xgc_cfg = cfg.get("xgc", {})

    run_id = run_id or new_run_id()
    job_name = job_name or f"xgc-{run_id}"
    warnings: list[str] = []

    nodes = nodes if nodes is not None else defaults.get("nodes", 1)
    walltime = walltime or defaults.get("walltime", "00:10:00")
    account = account or defaults.get("account")
    partition = partition or defaults.get("partition")
    qos = qos or defaults.get("qos")
    constraint = constraint or defaults.get("constraint")

    for label, value in (("account", account), ("partition", partition)):
        if m.is_placeholder(value):
            warnings.append(f"{label} is still a placeholder ({value})")

    if hello_world:
        exe = None
    else:
        exe = xgc_exe or xgc_cfg.get("exe")
        if not exe:
            raise SubmissionError(
                f"no XGC executable configured for {machine!r}. "
                f"Set xgc.exe in machines.yaml, pass xgc_exe, or use hello_world=True."
            )
        if m.is_placeholder(exe):
            raise SubmissionError(f"xgc.exe for {machine!r} is a placeholder: {exe}")

    run_root = cfg.get("paths", {}).get("run_root")
    if not run_root or m.is_placeholder(run_root):
        raise SubmissionError(f"paths.run_root for {machine!r} is not configured")
    remote_run_dir = f"{run_root}/{run_id}"

    ntasks_per_node = ntasks_per_node if ntasks_per_node is not None else 1
    total_ranks = nodes * ntasks_per_node

    template_name = Path(cfg["scheduler"]["submit_tpl"]).name
    script = _jinja_env().get_template(template_name).render(
        job_name=job_name,
        run_dir=remote_run_dir,
        nodes=nodes,
        walltime=walltime,
        account=account,
        partition=partition,
        qos=qos,
        constraint=constraint,
        ntasks_per_node=ntasks_per_node,
        gpus_per_node=None,
        total_ranks=total_ranks,
        modules=xgc_cfg.get("modules") or [],
        env_script=xgc_cfg.get("env_script"),
        xgc_exe=exe,
        generated_at=_now().isoformat(),
        provenance_id=run_id,
    )

    hours = _walltime_to_hours(walltime)
    resources = {
        "nodes": nodes,
        "ntasks_per_node": ntasks_per_node,
        "total_ranks": total_ranks,
        "walltime": walltime,
        "node_hours_max": round(nodes * hours, 2),
        "account": account,
        "partition": partition,
        "qos": qos,
        "scheduler": scheduler.type_name,
        "executable": exe or "(hello-world: srun hostname)",
    }

    return SubmissionPlan(
        machine=machine,
        run_id=run_id,
        job_name=job_name,
        script=script,
        remote_run_dir=remote_run_dir,
        remote_script_path=f"{remote_run_dir}/submit.sh",
        resources=resources,
        warnings=warnings,
    )


def _write_provenance(plan: SubmissionPlan, record: dict[str, Any]) -> Path:
    """Persist the run record locally under runs/<run_id>/."""
    run_dir = RUNS_DIR / plan.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "submit.sh").write_text(plan.script, encoding="utf-8")
    meta_path = run_dir / "provenance.json"
    meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return meta_path


def submit(
    machine: str,
    *,
    confirm: bool = False,
    plan: SubmissionPlan | None = None,
    case_dir: str | None = None,
    skip_dryrun: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Submit a job, but only with explicit confirmation.

    Without confirm=True this returns the plan with submitted=False and touches
    nothing remote. That is the human-in-the-loop gate from absolute rule 1.

    When case_dir is given, `xgc --dryrun` runs on the login node first and a
    rejected input blocks submission. It costs seconds and no allocation, so it
    is far cheaper than discovering a bad namelist after queueing. skip_dryrun
    forces past it; a caller doing that should have a reason.
    """
    plan = plan or prepare_submission(machine, **kwargs)

    if not confirm:
        return {
            "submitted": False,
            "reason": "confirmation required; review the plan and resubmit with confirm=True",
            "plan": plan.to_dict(),
        }

    dryrun_report: dict[str, Any] | None = None
    if case_dir and not skip_dryrun:
        report = dryrun.run_dryrun(
            plan.machine,
            case_dir,
            n_ranks=plan.resources.get("total_ranks"),
        )
        dryrun_report = report.to_dict()
        if report.blocking:
            return {
                "submitted": False,
                "reason": f"input validation failed: {report.summary()}",
                "dryrun": dryrun_report,
                "plan": plan.to_dict(),
            }

    cfg = m.get_machine(plan.machine)
    scheduler = sched.get_scheduler(cfg)
    host = ssh.host_for(cfg)

    ssh.mkdir_p(host, plan.remote_run_dir)
    ssh.write_file(host, plan.remote_script_path, plan.script)
    ssh.run(host, ["chmod", "+x", plan.remote_script_path], check=True)

    result = ssh.run(
        host,
        scheduler.submit_argv(plan.remote_script_path),
        cwd=plan.remote_run_dir,
        check=True,
    )
    job_id = scheduler.parse_job_id(result.stdout)

    record = {
        "run_id": plan.run_id,
        "machine": plan.machine,
        "job_id": job_id,
        "job_name": plan.job_name,
        "submitted_at": _now().isoformat(),
        "submitted_from": socket.gethostname(),
        "remote_run_dir": plan.remote_run_dir,
        "remote_script_path": plan.remote_script_path,
        "resources": plan.resources,
        "xgc": {
            "exe": cfg.get("xgc", {}).get("exe"),
            "commit": cfg.get("xgc", {}).get("verified", {}).get("commit"),
        },
        "scheduler_stdout": result.stdout.strip(),
        "case_dir": case_dir,
        "dryrun": dryrun_report,
    }
    meta_path = _write_provenance(plan, record)

    return {
        "submitted": True,
        "job_id": job_id,
        "run_id": plan.run_id,
        "machine": plan.machine,
        "remote_run_dir": plan.remote_run_dir,
        "provenance": str(meta_path.relative_to(PROJECT_ROOT)),
        "resources": plan.resources,
        "dryrun": dryrun_report,
    }


def status(machine: str, job_id: str) -> dict[str, Any]:
    """Query job state, normalized across schedulers.

    Slurm drops finished jobs from squeue, so an empty queue result triggers a
    sacct lookup to distinguish "completed" from "never existed".
    """
    cfg = m.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)
    host = ssh.host_for(cfg)

    result = ssh.run(host, scheduler.status_argv(job_id))
    state = scheduler.parse_status(result.stdout, result.returncode)

    out: dict[str, Any] = {
        "machine": machine,
        "job_id": job_id,
        "state": state,
        "source": scheduler.type_name,
    }

    if state == sched.UNKNOWN and isinstance(scheduler, sched.SlurmScheduler):
        acct = ssh.run(host, scheduler.accounting_argv(job_id))
        if acct.ok and acct.stdout.strip():
            acct_state, exit_code = scheduler.parse_accounting(acct.stdout)
            out["state"] = acct_state
            out["exit_code"] = exit_code
            out["source"] = "sacct"

    out["terminal"] = out["state"] in sched.TERMINAL_STATES
    return out


def cancel(machine: str, job_id: str, *, confirm: bool = False) -> dict[str, Any]:
    """Cancel a job. Requires confirmation, like submission."""
    if not confirm:
        return {
            "cancelled": False,
            "reason": "confirmation required; resubmit with confirm=True",
            "machine": machine,
            "job_id": job_id,
        }

    cfg = m.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)
    host = ssh.host_for(cfg)

    result = ssh.run(host, scheduler.cancel_argv(job_id))
    # Login shells print module-loading banners to stderr; that noise is not a
    # scheduler message, so report stderr only when the command actually failed.
    detail = result.stdout.strip() if result.ok else (result.stderr or result.stdout).strip()
    return {
        "cancelled": result.ok,
        "machine": machine,
        "job_id": job_id,
        "detail": detail or ("cancel request accepted" if result.ok else "cancel failed"),
    }


def fetch_output(machine: str, run_id: str, *, max_bytes: int = 100_000) -> dict[str, Any]:
    """Read the job's stdout/stderr from the remote run directory.

    Reads a bounded tail rather than pulling files, since XGC logs can be large
    and we only ever want a summary locally (AGENTS.md, data principles).
    """
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    run_root = cfg.get("paths", {}).get("run_root")
    remote_run_dir = f"{run_root}/{run_id}"

    listing = ssh.run(host, ["ls", "-1", remote_run_dir])
    if not listing.ok:
        return {"error": f"run directory not found: {remote_run_dir}"}

    files = [f for f in listing.stdout.split() if f.endswith((".out", ".err"))]
    contents: dict[str, str] = {}
    for name in files:
        res = ssh.run(host, ["tail", "-c", str(max_bytes), f"{remote_run_dir}/{name}"])
        if res.ok:
            contents[name] = res.stdout

    return {
        "machine": machine,
        "run_id": run_id,
        "remote_run_dir": remote_run_dir,
        "files": listing.stdout.split(),
        "contents": contents,
    }
