"""Job submission, monitoring, and cancellation.

Submission is deliberately staged (AGENTS.md, absolute rules 1 and 4):

    plan = prepare_submission(...)   # renders the script, estimates cost
    # human reviews plan["script"] and plan["resources"]
    submit(..., case_dir=..., confirm=True)
        # 1. xgc --dryrun validates the input on the login node (seconds, free)
        # 2. only if it passes does anything reach the scheduler

`submit` without `confirm=True` returns the plan and submits nothing. Both
gates live here rather than in the MCP layer so they hold no matter who calls.

Every submission writes a provenance record below runs/master/ or runs/test/,
including the dryrun report, so a run's validation state is recoverable later
(rule 5). When the job belongs to a registered simulation, the run is also
linked into that simulation's catalog record, so the number, the job, and the
directory stay connected without anyone writing them down (rule 5,
docs/simulation-catalog.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tools import dryrun
from tools import build_banner
from tools import machines as m
from tools import namelist as nlm
from tools import provenance as prov
from tools import scheduler as sched
from tools import simulation_catalog as catalog
from tools import simulation_registry as sim_registry
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
    simulation: dict[str, Any] | None
    provenance_class: str
    resources: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "run_id": self.run_id,
            "job_name": self.job_name,
            "remote_run_dir": self.remote_run_dir,
            "remote_script_path": self.remote_script_path,
            "simulation": self.simulation,
            "provenance_class": self.provenance_class,
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
    cpus_per_task: int | None = None,
    gpus_per_node: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    qos: str | None = None,
    constraint: str | None = None,
    mail_type: str | None = None,
    mail_user: str | None = None,
    xgc_exe: str | None = None,
    env_script: str | None = None,
    xgc_commit: str | None = None,
    run_id: str | None = None,
    simulation_id: str | None = None,
    provenance_class: str | None = None,
    hello_world: bool = False,
    run_dir: str | None = None,
    daos: bool = False,
) -> SubmissionPlan:
    """Render the job script and estimate cost. Submits nothing.

    Args:
        machine: key in machines.yaml.
        partition: Slurm partition or PBS queue.
        hello_world: ignore XGC and run hostname through the site launcher.
        xgc_exe: override the executable from machines.yaml.
        env_script: override the environment script from machines.yaml. Needed
            when a build is recorded under xgc.observed_builds rather than as
            the machine's default, since that build's modules live with it.
        xgc_commit: source commit corresponding to an executable override.
        provenance_class: local record class, ``master`` or ``test``. Registered
            simulations default to master; other jobs default to test.
        daos: run XGC inside the machine's DAOS POSIX container (Aurora). The
            case directory on the parallel filesystem keeps the input, the
            script, and the logs; XGC itself executes in
            ``<mount_root>/<pool>/<container>/<case basename>``, which persists
            across jobs so restart chains keep working. Needs a ``daos``
            section with ``pool`` and ``container`` in the machine definition.

    Unspecified resource arguments fall back to the machine's `defaults`.
    """
    cfg = m.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)  # raises on unconfirmed schedulers
    defaults = cfg.get("defaults", {})
    xgc_cfg = cfg.get("xgc", {})
    resolved_env_script = (
        env_script if env_script is not None else xgc_cfg.get("env_script")
    )

    run_id = run_id or new_run_id()
    if simulation_id and hello_world:
        raise SubmissionError("simulation_id cannot be used for a hello-world job")
    try:
        provenance_class = prov.classify(
            provenance_class,
            simulation_id=simulation_id,
            hello_world=hello_world,
        )
    except prov.ProvenanceError as exc:
        raise SubmissionError(str(exc)) from exc
    job_name = job_name or f"xgc-{simulation_id or run_id}"
    warnings: list[str] = []
    simulation: dict[str, Any] | None = None

    nodes = nodes if nodes is not None else defaults.get("nodes", 1)
    walltime = walltime or defaults.get("walltime", "00:10:00")
    account = account or defaults.get("account")
    partition = partition or defaults.get("partition") or defaults.get("queue")
    qos = qos or defaults.get("qos")
    constraint = constraint or defaults.get("constraint")
    mail_type = mail_type or defaults.get("mail_type")
    mail_user = mail_user or defaults.get("mail_user")
    filesystems = (
        defaults.get("filesystems")
        or cfg.get("scheduler", {}).get("filesystems")
    )
    pbs_system = cfg.get("scheduler", {}).get("system")

    for label, value in (("account", account), ("partition", partition)):
        if m.is_placeholder(value):
            warnings.append(f"{label} is still a placeholder ({value})")
    if m.is_placeholder(filesystems):
        warnings.append(f"filesystems is still a placeholder ({filesystems})")

    daos_cfg = _daos_settings(machine, cfg) if daos else None
    if daos_cfg and filesystems and daos_cfg["filesystems_resource"] not in str(
        filesystems
    ).split(":"):
        # The PBS prologue starts the DAOS agent only when the job asks for it.
        filesystems = f"{filesystems}:{daos_cfg['filesystems_resource']}"

    if hello_world:
        exe = None
        resolved_xgc_commit = None
    else:
        exe = xgc_exe or xgc_cfg.get("exe")
        if not exe:
            raise SubmissionError(
                f"no XGC executable configured for {machine!r}. "
                f"Set xgc.exe in machines.yaml, pass xgc_exe, or use hello_world=True."
            )
        if m.is_placeholder(exe):
            raise SubmissionError(f"xgc.exe for {machine!r} is a placeholder: {exe}")
        configured_exe = xgc_cfg.get("exe")
        resolved_xgc_commit = xgc_commit
        if resolved_xgc_commit is None and exe == configured_exe:
            resolved_xgc_commit = xgc_cfg.get("verified", {}).get("commit")

    # A reserved simulation has one canonical directory shared across its
    # submission attempts/restarts. The timestamp run_id remains separate and
    # identifies this particular scheduler submission in provenance.
    if simulation_id:
        try:
            directory = sim_registry.simulation_directory(simulation_id, machine)
            simulation = sim_registry.get_simulation(simulation_id)
        except sim_registry.SimulationRegistryError as exc:
            raise SubmissionError(str(exc)) from exc
        expected_dir = directory["remote_directory"].rstrip("/")
        if run_dir and run_dir.rstrip("/") != expected_dir:
            raise SubmissionError(
                f"case_dir/run_dir for {simulation_id} must be its canonical "
                f"directory {expected_dir!r}; got {run_dir.rstrip('/')!r}"
            )
        remote_run_dir = expected_dir
    elif run_dir:
        # Legacy/unregistered case: preserve the explicit path.
        remote_run_dir = run_dir.rstrip("/")
    else:
        run_root = cfg.get("paths", {}).get("run_root")
        if not run_root or m.is_placeholder(run_root):
            raise SubmissionError(f"paths.run_root for {machine!r} is not configured")
        remote_run_dir = f"{run_root}/{run_id}"

    ntasks_per_node = (
        ntasks_per_node
        if ntasks_per_node is not None
        else defaults.get("ntasks_per_node", 1)
    )
    cpus_per_task = (
        cpus_per_task
        if cpus_per_task is not None
        else defaults.get("cpus_per_task")
    )
    gpus_per_node = (
        gpus_per_node
        if gpus_per_node is not None
        else defaults.get("gpus_per_node")
    )
    try:
        scheduler.validate_resources(
            {
                "account": account,
                "queue": partition,
                "partition": partition,
                "qos": qos,
                "constraint": constraint,
                "mail_user": mail_user,
                "mail_type": mail_type,
                "filesystems": filesystems,
                "system": pbs_system,
            }
        )
    except sched.SchedulerError as exc:
        raise SubmissionError(str(exc)) from exc
    total_ranks = nodes * ntasks_per_node

    if daos_cfg:
        # One directory per case inside the container; the basename is the
        # canonical simulation directory name, so it is unique per simulation.
        daos_cfg["work_dir"] = (
            f"{daos_cfg['mount_root']}/{daos_cfg['pool']}/"
            f"{daos_cfg['container']}/{Path(remote_run_dir).name}"
        )

    template_name = Path(cfg["scheduler"]["submit_tpl"]).name
    script = _jinja_env().get_template(template_name).render(
        job_name=job_name,
        run_dir=remote_run_dir,
        nodes=nodes,
        walltime=walltime,
        account=account,
        partition=partition,
        queue=partition,
        qos=qos,
        constraint=constraint,
        mail_type=mail_type,
        mail_user=mail_user,
        filesystems=filesystems,
        pbs_system=pbs_system,
        ntasks_per_node=ntasks_per_node,
        cpus_per_task=cpus_per_task,
        gpus_per_node=gpus_per_node,
        gpu_aware_mpi=bool(xgc_cfg.get("gpu_aware_mpi", False)),
        total_ranks=total_ranks,
        modules=xgc_cfg.get("modules") or [],
        env_script=resolved_env_script,
        xgc_exe=exe,
        daos=daos_cfg,
        generated_at=_now().isoformat(),
        provenance_id=run_id,
    )

    hours = _walltime_to_hours(walltime)
    resources = {
        "nodes": nodes,
        "ntasks_per_node": ntasks_per_node,
        "cpus_per_task": cpus_per_task,
        "gpus_per_node": gpus_per_node,
        "total_ranks": total_ranks,
        "walltime": walltime,
        "node_hours_max": round(nodes * hours, 2),
        "account": account,
        "partition": partition,
        "filesystems": filesystems,
        "qos": qos,
        "mail_type": mail_type,
        "mail_user": mail_user,
        "scheduler": scheduler.type_name,
        "executable": exe or "(hello-world: hostname)",
        "env_script": resolved_env_script,
        "xgc_commit": resolved_xgc_commit,
        "daos": daos_cfg,
    }

    return SubmissionPlan(
        machine=machine,
        run_id=run_id,
        job_name=job_name,
        script=script,
        remote_run_dir=remote_run_dir,
        remote_script_path=f"{remote_run_dir}/submit.sh",
        simulation=simulation,
        provenance_class=provenance_class,
        resources=resources,
        warnings=warnings,
    )


# Site defaults for running inside a DAOS POSIX container (ALCF Aurora,
# docs.alcf.anl.gov/aurora/data-management/daos/daos-overview). A machine's
# ``daos`` section overrides any of them; pool and container have no default.
_DAOS_DEFAULTS = {
    "filesystems_resource": "daos_user_fs",
    "mount_root": "/tmp",
    "modulefiles": "/soft/modulefiles",
    "module": "daos",
    "launch": "launch-dfuse.sh",
    # libpil4dfs intercepts data and metadata calls (kernel bypass); libioil
    # would intercept data only.
    "interception_library": "/usr/lib64/libpil4dfs.so",
    # gpu_tile_compact.sh's udevadm call misbehaves under the interception
    # library, so ALCF ships a copy that sets LD_PRELOAD after the ZE mask.
    "tile_wrapper": "/soft/daos/tools/scripts/gpu_tile_compact_LD_PRELOAD.sh",
}


def _daos_settings(machine: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve the machine's DAOS section, or explain what is missing."""
    section = cfg.get("daos")
    if not isinstance(section, dict):
        raise SubmissionError(
            f"{machine!r} has no daos section in machines.yaml; DAOS runs are "
            f"only possible where a pool has been allocated to the project"
        )
    resolved = dict(_DAOS_DEFAULTS)
    resolved.update({k: v for k, v in section.items() if v is not None})
    for key in ("pool", "container"):
        value = resolved.get(key)
        if not value or m.is_placeholder(value):
            raise SubmissionError(
                f"daos.{key} for {machine!r} is not configured (got {value!r}); "
                f"set it in machines.local.yaml"
            )
    return resolved


_INPUT_FILE_DIR_RE = re.compile(
    r"^\s*sml_input_file_dir\s*=\s*['\"]([^'\"]*)['\"]", re.MULTILINE | re.IGNORECASE
)


def _daos_input_dir_problem(plan: SubmissionPlan, case_dir: str) -> str | None:
    """Why the input cannot run from inside the container, if it cannot.

    XGC resolves ``sml_input_file_dir`` relative to its working directory,
    which under DAOS is the container mount, not the case directory on the
    parallel filesystem. A relative path therefore fails only once the job is
    running, after the allocation has been charged. XGC also truncates the
    value at 65 characters (``character(len=65)`` in module.F90).
    """
    cfg = m.get_machine(plan.machine)
    host = ssh.host_for(cfg)
    result = ssh.run(host, ["cat", f"{case_dir.rstrip('/')}/input"])
    if not result.ok:
        return f"cannot read {case_dir}/input to check sml_input_file_dir"
    match = _INPUT_FILE_DIR_RE.search(result.stdout)
    value = match.group(1) if match else "./"
    if not value.startswith("/"):
        return (
            f"sml_input_file_dir={value!r} is relative; under DAOS XGC runs in "
            f"{plan.resources['daos']['work_dir']} where it cannot resolve. Use "
            f"an absolute path on the parallel filesystem (at most 65 characters)"
        )
    if len(value) > 65:
        return (
            f"sml_input_file_dir is {len(value)} characters; XGC truncates it at 65"
        )
    return None


def _write_provenance(
    plan: SubmissionPlan,
    record: dict[str, Any],
    *,
    input_text: str | None = None,
) -> Path:
    """Persist the run record under runs/master/ or runs/test/.

    The input namelist is copied in alongside it. It is a few kilobytes, and
    without it a provenance record can say which job ran but not what it ran --
    the remote case directory is routinely edited for the next attempt.
    """
    run_dir = prov.record_dir(RUNS_DIR, plan.run_id, plan.provenance_class)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "submit.sh").write_text(plan.script, encoding="utf-8")
    if input_text is not None:
        (run_dir / "input").write_text(input_text, encoding="utf-8")
    meta_path = run_dir / "provenance.json"
    meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return meta_path


def _capture_input(machine: str, case_dir: str | None) -> dict[str, Any]:
    """Read the submitted input namelist and hash it.

    Best effort: a failure here must never block a submission that already
    passed its dryrun, so problems are recorded rather than raised.
    """
    if not case_dir:
        return {"input_sha256": None, "input_text": None, "input_capture": "no case_dir"}
    try:
        cfg = m.get_machine(machine)
        host = ssh.host_for(cfg)
        result = ssh.run(host, ["cat", f"{case_dir.rstrip('/')}/input"])
    except (KeyError, ssh.SSHError) as exc:
        return {"input_sha256": None, "input_text": None, "input_capture": str(exc)}
    if not result.ok:
        return {
            "input_sha256": None,
            "input_text": None,
            "input_capture": f"cannot read {case_dir}/input",
        }
    text = result.stdout
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"input_sha256": digest, "input_text": text, "input_capture": "ok"}


def _unknown_input_provenance(
    parse_status: str, error: str | None = None
) -> dict[str, Any]:
    """A stable shape for old, absent, or unparseable inputs."""
    payload: dict[str, Any] = {
        "parse_status": parse_status,
        "parameters": {
            name: None for name in nlm.IMPORTANT_PROVENANCE_PARAMETERS
        },
        "derived": {
            "field_model": "unknown",
            "field_model_source": None,
            "marker_model": "unknown",
            "species": None,
        },
    }
    if error:
        payload["parse_error"] = str(error)[-500:]
    return payload


def _input_provenance(
    machine: str,
    case_dir: str | None,
    *,
    dryrun_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse the submitted input with XGC's own namelist parser, best effort."""
    if not case_dir:
        return _unknown_input_provenance("not_applicable")
    remote_path = f"{case_dir.rstrip('/')}/input"
    try:
        parsed = nlm.read_namelist(machine, remote_path)
        summary = nlm.provenance_input_summary(parsed)
        # A current XGC dryrun reports the effective field model after defaults
        # are applied. This is stronger evidence than guessing from an absent
        # switch, while old records without that evidence remain unknown.
        derived = summary["derived"]
        if derived["field_model"] == "unknown" and isinstance(dryrun_report, dict):
            parallelism = dryrun_report.get("parallelism") or {}
            effective = (
                parallelism.get("field_model")
                if isinstance(parallelism, dict)
                else None
            )
            if effective in {"electrostatic", "electromagnetic"}:
                derived["field_model"] = effective
                derived["field_model_source"] = "xgc --dryrun"
        return summary
    except (KeyError, nlm.NamelistError, TypeError, ValueError) as exc:
        # Submission has already been accepted. Record uncertainty rather than
        # turning a provenance enrichment failure into an apparent job failure.
        return _unknown_input_provenance("unavailable", str(exc))


def _display_path(path: Path) -> str:
    """Project-relative when possible, absolute otherwise (redirected RUNS_DIR)."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _link_catalog(record: dict[str, Any], meta_path: Path) -> dict[str, Any] | None:
    """Attach a submitted run to its simulation's catalog record.

    Only registered simulations have one. A catalog problem must not turn an
    accepted submission into an apparent failure, so it is reported in the
    result instead of raised.
    """
    simulation_id = record.get("simulation_id")
    if not simulation_id:
        return None
    try:
        catalog.attach_run(
            simulation_id,
            {
                "run_id": record["run_id"],
                "machine": record["machine"],
                "job_id": record["job_id"],
                "submitted_at": record["submitted_at"],
                "provenance": _display_path(meta_path),
                "remote_run_dir": record["remote_run_dir"],
                "input_sha256": record.get("input_sha256"),
                "input": record.get("input"),
                "case": (record.get("simulation") or {}).get("case"),
            },
            status="submitted",
        )
        return {"simulation_id": simulation_id, "linked": True}
    except catalog.SimulationCatalogError as exc:
        return {"simulation_id": simulation_id, "linked": False, "error": str(exc)}


def _xgc_provenance(plan: SubmissionPlan) -> dict[str, Any]:
    """Record the build selected by the reviewed submission plan."""
    executable = plan.resources.get("executable")
    if executable == "(hello-world: hostname)":
        executable = None
    return {
        "exe": executable,
        "env_script": plan.resources.get("env_script"),
        "commit": plan.resources.get("xgc_commit"),
    }


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
    if plan is None:
        # A real XGC run must execute in its case directory; default to it.
        kwargs.setdefault("run_dir", case_dir)
        plan = prepare_submission(machine, **kwargs)

    if not confirm:
        return {
            "submitted": False,
            "reason": "confirmation required; review the plan and resubmit with confirm=True",
            "plan": plan.to_dict(),
        }

    dryrun_report: dict[str, Any] | None = None
    if case_dir and not skip_dryrun:
        # Validate with the same executable the job will run, not the
        # machine default, which may be unset or a different build.
        report = dryrun.run_dryrun(
            plan.machine,
            case_dir,
            n_ranks=plan.resources.get("total_ranks"),
            xgc_exe=plan.resources.get("executable"),
            env_script=plan.resources.get("env_script"),
        )
        dryrun_report = report.to_dict()
        if report.blocking:
            return {
                "submitted": False,
                "reason": f"input validation failed: {report.summary()}",
                "dryrun": dryrun_report,
                "plan": plan.to_dict(),
            }

    if case_dir and plan.resources.get("daos"):
        problem = _daos_input_dir_problem(plan, case_dir)
        if problem:
            return {
                "submitted": False,
                "reason": f"DAOS input check failed: {problem}",
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

    captured = _capture_input(plan.machine, case_dir)
    input_provenance = _input_provenance(
        plan.machine, case_dir, dryrun_report=dryrun_report
    )
    record = {
        "run_id": plan.run_id,
        "machine": plan.machine,
        "job_id": job_id,
        "job_name": plan.job_name,
        "submitted_at": _now().isoformat(),
        "submitted_from": socket.gethostname(),
        "remote_run_dir": plan.remote_run_dir,
        "remote_script_path": plan.remote_script_path,
        "simulation_id": (
            plan.simulation.get("simulation_id") if plan.simulation else None
        ),
        "simulation": plan.simulation,
        "provenance_class": plan.provenance_class,
        "resources": plan.resources,
        "xgc": _xgc_provenance(plan),
        "scheduler_stdout": result.stdout.strip(),
        "case_dir": case_dir,
        "input_sha256": captured["input_sha256"],
        "input_capture": captured["input_capture"],
        "input": input_provenance,
        "dryrun": dryrun_report,
    }
    meta_path = _write_provenance(plan, record, input_text=captured["input_text"])
    catalog_link = _link_catalog(record, meta_path)

    return {
        "submitted": True,
        "job_id": job_id,
        "run_id": plan.run_id,
        "machine": plan.machine,
        "remote_run_dir": plan.remote_run_dir,
        "provenance": _display_path(meta_path),
        "resources": plan.resources,
        "input_sha256": captured["input_sha256"],
        "input": input_provenance,
        "catalog": catalog_link,
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
    exit_code = scheduler.parse_exit_code(result.stdout)
    if exit_code is not None:
        out["exit_code"] = exit_code

    if state == sched.UNKNOWN and isinstance(scheduler, sched.SlurmScheduler):
        acct = ssh.run(host, scheduler.accounting_argv(job_id))
        if acct.ok and acct.stdout.strip():
            acct_state, exit_code = scheduler.parse_accounting(acct.stdout)
            out["state"] = acct_state
            out["exit_code"] = exit_code
            out["source"] = "sacct"

    out["terminal"] = out["state"] in sched.TERMINAL_STATES
    catalog_update = _record_observed_state(machine, job_id, out)
    if catalog_update is not None:
        out["catalog"] = catalog_update
    return out


def _find_run_for_job(machine: str, job_id: str) -> tuple[str, str] | None:
    """Map a scheduler job back to (simulation_id, run_id) via provenance."""
    if not RUNS_DIR.is_dir():
        return None
    for _class, path in prov.iter_records(RUNS_DIR):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("machine") != machine:
            continue
        if str(record.get("job_id")) != str(job_id):
            continue
        simulation_id = record.get("simulation_id")
        run_id = record.get("run_id")
        if simulation_id and run_id:
            return str(simulation_id), str(run_id)
    return None


def maybe_capture_build(
    machine: str, simulation_id: str, run_id: str, state: str
) -> dict[str, Any] | None:
    """Capture the XGC build banner from stdout, once per run.

    Called from status checks (and refresh) because that is the first moment
    the stdout file exists. Skips runs whose catalog entry already carries a
    ``build`` result. A definitive "no banner" is only recorded once the job
    is terminal -- while it runs, absence may just mean XGC has not started
    printing yet. Best effort throughout: build capture must never break a
    status query.
    """
    if state not in ("running",) and state not in sched.TERMINAL_STATES:
        return None
    try:
        record = catalog.read_record(simulation_id)
    except catalog.SimulationCatalogError:
        return None
    run_entry = next(
        (
            run
            for run in record.get("runs") or []
            if isinstance(run, dict) and run.get("run_id") == run_id
        ),
        None,
    )
    if run_entry is None or run_entry.get("build") is not None:
        return None
    remote_run_dir = run_entry.get("remote_run_dir")
    if not remote_run_dir:
        return None

    try:
        result = build_banner.capture(
            machine, str(remote_run_dir), run_entry.get("job_id")
        )
    except ssh.SSHError as exc:
        return {"captured": False, "reason": str(exc)}

    terminal = state in sched.TERMINAL_STATES
    if result.get("found"):
        summary = build_banner.catalog_summary(result)
    elif result.get("definitive") and terminal:
        # Stdout is complete and holds no banner (e.g. not an XGC job).
        # Record that so refresh stops re-reading the file forever.
        summary = {"captured": False, "reason": result.get("reason")}
    else:
        return {"captured": False, "reason": result.get("reason"), "retry": True}

    try:
        catalog.set_run_build(simulation_id, run_id, summary)
    except catalog.SimulationCatalogError as exc:
        return {"captured": False, "reason": str(exc)}

    if result.get("found"):
        _store_build_provenance(run_id, result)
    return {"captured": bool(result.get("found")), "build": summary}


def _store_build_provenance(run_id: str, result: dict[str, Any]) -> None:
    """Write the full banner into the run's provenance.json, when one exists.

    Adopted and externally submitted runs may have no local record; the
    catalog entry is then the only copy, which is fine. When the plan recorded
    an expected commit, a mismatch is flagged rather than silently kept: it
    means the binary was rebuilt between plan and execution.
    """
    try:
        found = prov.find_record(RUNS_DIR, run_id)
    except prov.ProvenanceError:
        return
    if not found:
        return
    _, provenance_path = found
    try:
        record = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(record, dict):
        return
    xgc = record.get("xgc")
    if not isinstance(xgc, dict):
        xgc = {}
    banner = result["banner"]
    xgc["build"] = banner
    xgc["build_source"] = {
        "file": result.get("source_file"),
        "captured_at": result.get("captured_at"),
    }
    planned = xgc.get("commit")
    observed = (banner.get("git") or {}).get("commit")
    if planned and observed and planned != observed:
        xgc["build_commit_mismatch"] = {
            "planned": planned,
            "observed": observed,
        }
    record["xgc"] = xgc
    try:
        provenance_path.write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        return


def _record_observed_state(
    machine: str, job_id: str, out: dict[str, Any]
) -> dict[str, Any] | None:
    """Push an observed job state into the simulation's catalog record.

    This is what closes the loop that used to be closed by hand: checking on a
    job is also what updates the simulation. The catalog decides what the
    state means for the simulation as a whole, since that depends on the other
    runs in the restart chain. Best effort, for the same reason as the
    submission link -- a status query must still answer even if the catalog
    cannot be written.
    """
    found = _find_run_for_job(machine, job_id)
    if found is None:
        return None
    simulation_id, run_id = found
    try:
        record = catalog.update_run_state(
            simulation_id,
            run_id,
            state=out["state"],
            exit_code=out.get("exit_code"),
            terminal=bool(out.get("terminal")),
        )
    except catalog.SimulationCatalogError as exc:
        return {"simulation_id": simulation_id, "updated": False, "error": str(exc)}
    result = {
        "simulation_id": simulation_id,
        "run_id": run_id,
        "updated": True,
        "simulation_status": record.get("status"),
    }
    build = maybe_capture_build(machine, simulation_id, run_id, out["state"])
    if build is not None:
        result["build_capture"] = build
    return result


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


def _output_run_dir(machine: str, run_id: str, cfg: dict[str, Any]) -> str:
    """Resolve a run's remote directory, preferring its provenance record.

    Real XGC jobs normally execute in a prepared case directory rather than
    ``paths.run_root/run_id``. Submission records that exact directory in
    provenance.json, so output lookup must follow the record instead of
    reconstructing a path that may never have existed.
    """
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise SubmissionError(f"invalid run_id {run_id!r}")

    try:
        found = prov.find_record(RUNS_DIR, run_id)
    except prov.ProvenanceError as exc:
        raise SubmissionError(str(exc)) from exc
    if found:
        _, provenance_path = found
        try:
            record = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SubmissionError(
                f"cannot read provenance for {run_id!r}: {exc}"
            ) from exc

        recorded_machine = record.get("machine")
        if recorded_machine and recorded_machine != machine:
            raise SubmissionError(
                f"run {run_id!r} belongs to {recorded_machine!r}, not {machine!r}"
            )
        recorded_dir = record.get("remote_run_dir")
        if isinstance(recorded_dir, str) and recorded_dir.strip():
            return recorded_dir.rstrip("/")

    run_root = cfg.get("paths", {}).get("run_root")
    if not run_root or m.is_placeholder(run_root):
        raise SubmissionError(f"paths.run_root for {machine!r} is not configured")
    return f"{run_root.rstrip('/')}/{run_id}"


def fetch_output(machine: str, run_id: str, *, max_bytes: int = 100_000) -> dict[str, Any]:
    """Read the job's stdout/stderr from the remote run directory.

    Reads a bounded tail rather than pulling files, since XGC logs can be large
    and we only ever want a summary locally (AGENTS.md, data principles).
    """
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    try:
        remote_run_dir = _output_run_dir(machine, run_id, cfg)
    except SubmissionError as exc:
        return {"error": str(exc)}

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
