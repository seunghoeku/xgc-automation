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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from tools import dryrun
from tools import build_banner
from tools import machines as m
from tools import namelist as nlm
from tools import notebook_csv
from tools import provenance as prov
from tools import scheduler as sched
from tools import simulation_catalog as catalog
from tools import simulation_registry as sim_registry
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
RUNS_DIR = PROJECT_ROOT / "runs"
# Every submitted input is also appended here under the simulation's directory
# name, the same layout as the historical archive (docs/input-archive.md).
INPUT_BACKUP_DIR = PROJECT_ROOT / "input_backup"


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
    xgc_args: str | None = None,
    spare_nodes: int = 0,
    depend_on: str | None = None,
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
        xgc_args: extra command-line arguments for the executable, e.g.
            ``--dryrun -n_ranks 128`` for a compute-node dryrun job.
        daos: run XGC inside the machine's DAOS POSIX container (Aurora). The
            case directory on the parallel filesystem keeps the input, the
            script, and the logs; XGC itself executes in
            ``<mount_root>/<pool>/<container>/<case basename>``, which persists
            across jobs so restart chains keep working. Needs a ``daos``
            section with ``pool`` and ``container`` in the machine definition.
        spare_nodes: request this many nodes beyond ``nodes`` and let the job
            script pick the healthy ones. Before launching XGC every allocated
            node is probed for its GPUs and the launcher gets a hostfile of the
            first ``nodes`` nodes that expose all of them; a crash within the
            first minutes drops the nodes that reported dead ranks and relaunches
            once on the remaining healthy ones. Rank count stays ``nodes *
            ntasks_per_node``; the spare nodes idle. Only templates that render
            the GPU gate accept it (Polaris).
        depend_on: scheduler job id this job must wait for. Rendered as an
            ``afterany`` dependency (PBS ``-W depend=afterany:<id>``, Slurm
            ``--dependency=afterany:<id>``), so the new job starts once that
            job has ended in any state. This is how a restart is chained
            behind the job still running in the same case directory without
            the two ever overlapping.

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
    if spare_nodes < 0:
        raise SubmissionError("spare_nodes must be >= 0")
    if spare_nodes and hello_world:
        raise SubmissionError("spare_nodes has no effect on a hello-world job")
    if spare_nodes and not gpus_per_node:
        raise SubmissionError(
            "spare_nodes needs gpus_per_node: the GPU gate probes each node for "
            "that many devices"
        )
    requested_nodes = nodes + spare_nodes
    depend_on = (depend_on or "").strip() or None
    if depend_on is not None and (
        " " in depend_on or ":" in depend_on or not depend_on[0].isdigit()
    ):
        raise SubmissionError(
            f"depend_on must be a single scheduler job id (got {depend_on!r}); "
            "the afterany dependency type is added by the template"
        )

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
        nodes=requested_nodes,
        compute_nodes=nodes,
        spare_nodes=spare_nodes,
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
        srun_cpu_binding=bool(cfg.get("scheduler", {}).get("srun_cpu_binding", False)),
        total_ranks=total_ranks,
        modules=xgc_cfg.get("modules") or [],
        env_script=resolved_env_script,
        xgc_exe=exe,
        xgc_args=f" {xgc_args.strip()}" if xgc_args else "",
        daos=daos_cfg,
        depend_on=depend_on,
        generated_at=_now().isoformat(),
        provenance_id=run_id,
    )
    if depend_on and f"afterany:{depend_on}" not in script:
        raise SubmissionError(
            f"depend_on is not supported by the {template_name} template"
        )

    if spare_nodes and "GPU gate" not in script:
        raise SubmissionError(
            f"spare_nodes is not supported by the {template_name} template "
            "(no GPU gate); only the Polaris template implements it"
        )

    hours = _walltime_to_hours(walltime)
    resources = {
        "nodes": requested_nodes,
        "compute_nodes": nodes,
        "spare_nodes": spare_nodes,
        "ntasks_per_node": ntasks_per_node,
        "cpus_per_task": cpus_per_task,
        "gpus_per_node": gpus_per_node,
        "total_ranks": total_ranks,
        "walltime": walltime,
        "node_hours_max": round(requested_nodes * hours, 2),
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
        "xgc_args": xgc_args,
        "depend_on": depend_on,
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


# XGC declares sml_input_file_dir as character(len=65) (module.F90), so a
# longer value is silently truncated and the run dies much later with a Fortran
# "Cannot open file" on a path that ends mid-directory. `xgc --dryrun` returns
# before the equilibrium profiles are read and accepts it, so this is the only
# gate that catches it (d3dqh dt=0 check, 2026-09-15: an 87-character $PSCRATCH
# path cut to .../sangjun_turb_QH_newgrid_Frontier_emphase/resta).
INPUT_FILE_DIR_MAX_CHARS = 65


def _input_file_dir_problem(
    plan: SubmissionPlan, case_dir: str, *, require_absolute: bool
) -> str | None:
    """Why ``sml_input_file_dir`` would fail at run time, if it would.

    The length limit is XGC's own and applies on every machine. Absoluteness is
    required only under DAOS: XGC resolves the value against its working
    directory, which there is the container mount rather than the case
    directory on the parallel filesystem, so a relative path fails once the job
    is running and the allocation has been charged.
    """
    cfg = m.get_machine(plan.machine)
    host = ssh.host_for(cfg)
    result = ssh.run(host, ["cat", f"{case_dir.rstrip('/')}/input"])
    if not result.ok:
        return f"cannot read {case_dir}/input to check sml_input_file_dir"
    match = _INPUT_FILE_DIR_RE.search(result.stdout)
    value = match.group(1) if match else "./"
    if require_absolute and not value.startswith("/"):
        return (
            f"sml_input_file_dir={value!r} is relative; under DAOS XGC runs in "
            f"{plan.resources['daos']['work_dir']} where it cannot resolve. Use "
            f"an absolute path on the parallel filesystem "
            f"(at most {INPUT_FILE_DIR_MAX_CHARS} characters)"
        )
    if len(value) > INPUT_FILE_DIR_MAX_CHARS:
        return (
            f"sml_input_file_dir is {len(value)} characters; XGC truncates it "
            f"at {INPUT_FILE_DIR_MAX_CHARS} and the job fails at run time "
            f"reading the equilibrium profiles (xgc --dryrun does not catch "
            f"this). Shorten it, e.g. a relative '../XGC_inputs/' beside the "
            f"run directory. Value: {value!r}"
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


# CMakeCache entries worth keeping with a run: what the binary was built from
# and how. Anything matching is copied verbatim; the list is deliberately short
# so the block stays readable in a YAML record.
_CMAKE_PROVENANCE_RE = re.compile(
    r"^(CMAKE_BUILD_TYPE|CMAKE_(?:CXX|Fortran|CUDA)_COMPILER|CMAKE_(?:CXX|Fortran)_FLAGS"
    r"|XGC_[A-Z0-9_]+|USE_[A-Z0-9_]+|BUILD_[A-Z0-9_]+"
    r"|Kokkos_DIR|ADIOS2_DIR|PETSC_DIR|Ginkgo_DIR|Cabana_DIR):[A-Z]+=(.*)$"
)


def _reproduction_script(
    case_dir: str,
    input_file_dir: str | None,
    names: list[str],
    env_script: str | None,
    build_dir: str | None,
) -> str:
    """One remote bash script that hashes everything a rerun would need.

    Output is line-oriented and tab-separated so it parses without ambiguity:
    ``FILE name sha size mtime`` (or ``FILE name MISSING``), ``ENV sha`` for the
    environment script, and ``CMAKE key:type=value`` lines from the build cache.
    """
    import shlex

    lines = [f"cd {shlex.quote(case_dir)} || exit 0"]
    lines.append(f"d={shlex.quote(input_file_dir or '.')}")
    for name in names:
        q = shlex.quote(name)
        lines.append(
            f'p="$d"/{q}; if [ -f "$p" ]; then printf \'FILE\\t%s\\t%s\\t%s\\t%s\\n\' {q} '
            f'"$(sha256sum "$p" | cut -d\' \' -f1)" "$(stat -c %s "$p")" '
            f'"$(date -u -d @"$(stat -c %Y "$p")" +%Y-%m-%dT%H:%M:%SZ)"; '
            f"else printf 'FILE\\t%s\\tMISSING\\n' {q}; fi"
        )
    if env_script:
        q = shlex.quote(env_script)
        lines.append(
            f"[ -f {q} ] && printf 'ENV\\t%s\\n' \"$(sha256sum {q} | cut -d' ' -f1)\""
        )
    if build_dir:
        q = shlex.quote(build_dir.rstrip("/") + "/CMakeCache.txt")
        lines.append(
            f"[ -f {q} ] && grep -E '^(CMAKE_BUILD_TYPE|CMAKE_(CXX|Fortran|CUDA)_COMPILER"
            f"|CMAKE_(CXX|Fortran)_FLAGS|XGC_|USE_|BUILD_|Kokkos_DIR|ADIOS2_DIR|PETSC_DIR"
            f"|Ginkgo_DIR|Cabana_DIR)' {q} | sed 's/^/CMAKE\\t/'"
        )
    lines.append("exit 0")
    return "\n".join(lines)


def _parse_reproduction_output(stdout: str) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    env_sha: str | None = None
    cmake: dict[str, str] = {}
    for raw in stdout.splitlines():
        parts = raw.rstrip("\n").split("\t")
        if parts[0] == "FILE" and len(parts) >= 3:
            name = parts[1]
            if parts[2] == "MISSING":
                missing.append(name)
            elif len(parts) >= 5:
                try:
                    size: int | None = int(parts[3])
                except ValueError:
                    size = None
                files[name] = {"sha256": parts[2], "size_bytes": size, "mtime": parts[4]}
        elif parts[0] == "ENV" and len(parts) >= 2:
            env_sha = parts[1]
        elif parts[0] == "CMAKE" and len(parts) >= 2:
            match = _CMAKE_PROVENANCE_RE.match(parts[1])
            if match:
                cmake[match.group(1)] = match.group(2)
    return {"files": files, "missing": missing, "env_sha256": env_sha, "cmake": cmake}


def _reproduction_provenance(
    machine: str,
    case_dir: str | None,
    input_text: str | None,
    resources: dict[str, Any],
) -> dict[str, Any]:
    """Hash the files the input names and describe the build. Best effort.

    A namelist alone does not pin a simulation: the mesh, equilibrium and
    profile files it names are edited in place, and the binary's build settings
    are not in the banner. Both are read in one remote command right after
    submission. Any failure is recorded, never raised -- the job is already
    queued.
    """
    result: dict[str, Any] = {"referenced_inputs": None, "build_environment": None}
    if not case_dir or not input_text:
        return result
    refs = nlm.referenced_input_files(input_text)
    names = sorted({name for items in refs["files"].values() for name in items})
    parameters_for: dict[str, list[str]] = {}
    for parameter, items in refs["files"].items():
        for name in items:
            parameters_for.setdefault(name, []).append(parameter)

    cfg = m.get_machine(machine)
    env_script = resources.get("env_script")
    build_dir = resources.get("build_dir") or (cfg.get("xgc") or {}).get("build")
    script = _reproduction_script(case_dir, refs["input_file_dir"], names, env_script, build_dir)
    try:
        output = ssh.run(ssh.host_for(cfg), ["bash", "-c", script], timeout=180)
    except ssh.SSHError as exc:
        result["error"] = str(exc)
        return result
    parsed = _parse_reproduction_output(output.stdout)
    checked = _now().isoformat()

    files = {
        name: {"parameters": parameters_for.get(name, []), **info}
        for name, info in parsed["files"].items()
    }
    result["referenced_inputs"] = {
        "case_dir": case_dir,
        "input_file_dir": refs["input_file_dir"],
        "files": files,
        "missing": parsed["missing"],
        "hashed_at": checked,
    }
    result["build_environment"] = {
        "executable": resources.get("executable"),
        "xgc_commit": resources.get("xgc_commit"),
        "env_script": {"path": env_script, "sha256": parsed["env_sha256"]},
        "cmake_cache": f"{build_dir.rstrip('/')}/CMakeCache.txt" if build_dir else None,
        "cmake": parsed["cmake"],
        "captured_at": checked,
    }
    return result


def _backup_input(
    simulation: dict[str, Any] | None,
    input_text: str | None,
    *,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Append the submitted input to input_backup/ under the directory name.

    The archive's convention is one file per simulation named after its run
    directory. The first submission claims that name; a later submission whose
    input differs (a restart with a changed parameter) is kept as a dated
    variant, ``<directory_name>.Sep10``, which ``tools.input_archive`` already
    recognises as a variant rather than a second simulation. Identical inputs
    write nothing. Never raises: a local copy failure must not mask an accepted
    submission.
    """
    directory_name = (simulation or {}).get("directory_name")
    if not directory_name or input_text is None:
        return {"written": False, "reason": "no simulation directory name or no input"}
    try:
        INPUT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        primary = INPUT_BACKUP_DIR / str(directory_name)
        if not primary.exists():
            primary.write_text(input_text, encoding="utf-8")
            return {"written": True, "path": _display_path(primary)}
        if primary.read_text(encoding="utf-8") == input_text:
            return {"written": False, "reason": "identical", "path": _display_path(primary)}
        stamp = (when or _now()).strftime("%b%d")
        variant = INPUT_BACKUP_DIR / f"{directory_name}.{stamp}"
        attempt = 1
        while variant.exists() and variant.read_text(encoding="utf-8") != input_text:
            attempt += 1
            variant = INPUT_BACKUP_DIR / f"{directory_name}.{stamp}.bak{attempt}"
        if not variant.exists():
            variant.write_text(input_text, encoding="utf-8")
            return {"written": True, "path": _display_path(variant), "variant_of": _display_path(primary)}
        return {"written": False, "reason": "identical", "path": _display_path(variant)}
    except OSError as exc:
        return {"written": False, "error": str(exc)}


def _sync_notebook() -> dict[str, Any]:
    """Rewrite the CSV notebook from the catalog. Best effort, never raises."""
    try:
        payload = notebook_csv.sync(confirm=True)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return {"synced": False, "error": str(exc)}
    return {
        "synced": True,
        "path": payload.get("path"),
        "rows": payload.get("pushed_rows"),
        "pulled_notes": payload.get("pulled_notes", []),
    }


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
                "referenced_inputs": record.get("referenced_inputs"),
                "build_environment": record.get("build_environment"),
                "input_backup": record.get("input_backup"),
                "dryrun_parallel": record.get("dryrun_parallel"),
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


def dryrun_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """How this machine validates an input.

    ``login_node: true`` (the default) means ``xgc --dryrun`` runs on the login
    node before submission and a rejection blocks the job. ``login_node:
    false`` (Polaris, Aurora: the CUDA/oneAPI build cannot start without a
    device or launcher) means the job is submitted first and the dryrun runs
    as a parallel 1-node job only when the production job is still waiting
    after ``start_grace_seconds``.
    """
    raw = cfg.get("dryrun") or {}
    queues: list[dict[str, Any]] = []
    for item in raw.get("queues") or ([{"name": raw["queue"]}] if raw.get("queue") else []):
        if isinstance(item, str):
            item = {"name": item}
        if isinstance(item, dict) and item.get("name"):
            queues.append(
                {
                    "name": str(item["name"]),
                    "min_nodes": int(item.get("min_nodes", 1)),
                    "max_nodes": int(item["max_nodes"]) if item.get("max_nodes") else None,
                    "per_user": int(item["per_user"]) if item.get("per_user") else None,
                }
            )
    return {
        "login_node": bool(raw.get("login_node", True)),
        "queues": queues,
        "nodes": int(raw.get("nodes", 1)),
        "walltime": str(raw.get("walltime", "00:15:00")),
        "start_grace_seconds": float(raw.get("start_grace_seconds", 20)),
    }


def _user_queue_occupancy(
    cfg: dict[str, Any], scheduler: sched.Scheduler, host: str
) -> dict[str, int]:
    """How many of the user's jobs sit in each queue right now (queued or running).

    Per-user queue limits count queued jobs too, so a dryrun sent to a queue
    the user already occupies would wait behind their own job -- or be
    rejected outright. PBS needs two calls (ids, then details in JSON); Slurm
    answers in one.
    """
    user = cfg.get("user")
    if not user or m.is_placeholder(user):
        return {}
    counts: dict[str, int] = {}
    if isinstance(scheduler, sched.PBSScheduler):
        ids = ssh.run(host, scheduler.user_queue_argv(str(user)))
        job_ids = [j["job_id"] for j in scheduler.parse_user_queue(ids.stdout)] if ids.ok else []
        argv = scheduler.user_queue_detail_argv(job_ids)
        if not argv:
            return {}
        detail = ssh.run(host, argv)
        if not detail.ok:
            return {}
        for job in scheduler.parse_user_queue_detail(detail.stdout).values():
            if job.get("state") in (sched.PENDING, sched.RUNNING) and job.get("queue"):
                counts[job["queue"]] = counts.get(job["queue"], 0) + 1
    else:
        listing = ssh.run(host, ["squeue", "-u", str(user), "-h", "-o", "%P|%T"])
        if not listing.ok:
            return {}
        for line in listing.stdout.splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            state = sched._SLURM_STATES.get(parts[1].strip().upper(), sched.UNKNOWN)
            if state in (sched.PENDING, sched.RUNNING):
                counts[parts[0].strip()] = counts.get(parts[0].strip(), 0) + 1
    return counts


def choose_dryrun_queue(
    settings: dict[str, Any], occupancy: dict[str, int], nodes: int
) -> dict[str, Any]:
    """The first configured queue that can take an ``nodes``-node job now.

    A queue is skipped when the user already holds its per-user job limit
    (queued jobs count) or when the job does not fit its node range. The
    reasons are returned so the provenance can say why the dryrun ended up in
    ``preemptable`` rather than ``debug``.
    """
    skipped: dict[str, str] = {}
    for queue in settings["queues"]:
        name = queue["name"]
        if nodes < queue["min_nodes"]:
            skipped[name] = f"needs at least {queue['min_nodes']} node(s)"
            continue
        if queue["max_nodes"] is not None and nodes > queue["max_nodes"]:
            skipped[name] = f"allows at most {queue['max_nodes']} node(s)"
            continue
        held = occupancy.get(name, 0)
        if queue["per_user"] is not None and held >= queue["per_user"]:
            skipped[name] = f"user already has {held} job(s) there (limit {queue['per_user']})"
            continue
        return {"queue": name, "skipped": skipped, "occupancy": occupancy}
    return {"queue": None, "skipped": skipped, "occupancy": occupancy}


def _start_parallel_dryrun(
    plan: SubmissionPlan,
    cfg: dict[str, Any],
    scheduler: sched.Scheduler,
    host: str,
    job_id: str,
    case_dir: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Validate the input beside a production job that cannot be dryrun first.

    Waits ``start_grace_seconds`` and asks the scheduler about the production
    job. If it is already running, the run itself is the validation and no
    second job is spent. Otherwise a 1-node job in the debug queue runs
    ``xgc --dryrun -n_ranks <production ranks>`` in the same case directory;
    its stdout is read and parsed on the next status check
    (``maybe_capture_parallel_dryrun``). Never raises: the production job is
    already queued, so a problem here is reported, not thrown.
    """
    grace = settings["start_grace_seconds"]
    base: dict[str, Any] = {
        "mode": "parallel",
        "reason": f"xgc --dryrun cannot run on a {plan.machine} login node",
        "grace_seconds": grace,
    }
    try:
        if grace > 0:
            time.sleep(grace)
        probe = ssh.run(host, scheduler.status_argv(job_id))
        state = scheduler.parse_status(probe.stdout, probe.returncode)
    except ssh.SSHError as exc:
        state = sched.UNKNOWN
        base["probe_error"] = str(exc)
    base["main_job_state_after_grace"] = state
    if state == sched.RUNNING or state in sched.TERMINAL_STATES:
        base.update(
            started=False,
            detail=(
                "the production job started within the grace period; "
                "the run itself validates the input"
            ),
        )
        return base

    try:
        occupancy = _user_queue_occupancy(cfg, scheduler, host)
    except ssh.SSHError as exc:
        occupancy = {}
        base["occupancy_error"] = str(exc)
    choice = choose_dryrun_queue(settings, occupancy, settings["nodes"])
    base["queue_choice"] = {"skipped": choice["skipped"], "occupancy": occupancy}
    if choice["queue"] is None:
        base.update(
            started=False,
            error=(
                "no configured queue can take a "
                f"{settings['nodes']}-node dryrun job right now: {choice['skipped']}"
            ),
        )
        return base

    try:
        dry_plan = prepare_submission(
            plan.machine,
            run_id=f"{plan.run_id}-dryrun",
            job_name=f"{plan.job_name}-dryrun",
            nodes=settings["nodes"],
            ntasks_per_node=1,
            cpus_per_task=plan.resources.get("cpus_per_task"),
            gpus_per_node=plan.resources.get("gpus_per_node"),
            walltime=settings["walltime"],
            account=plan.resources.get("account"),
            partition=choice["queue"],
            xgc_exe=plan.resources.get("executable"),
            env_script=plan.resources.get("env_script"),
            xgc_commit=plan.resources.get("xgc_commit"),
            run_dir=case_dir,
            provenance_class="test",
            daos=bool(plan.resources.get("daos")),
            xgc_args=f"--dryrun -n_ranks {plan.resources.get('total_ranks')}",
        )
        # The production script owns <case_dir>/submit.sh; keep the dryrun's
        # script beside it under its own name.
        dry_plan.remote_script_path = f"{case_dir.rstrip('/')}/dryrun-{plan.run_id}.sh"
        ssh.write_file(host, dry_plan.remote_script_path, dry_plan.script)
        ssh.run(host, ["chmod", "+x", dry_plan.remote_script_path], check=True)
        submitted = ssh.run(
            host,
            scheduler.submit_argv(dry_plan.remote_script_path),
            cwd=case_dir,
            check=True,
            timeout=SUBMIT_TIMEOUT_SECONDS,
        )
        dry_job = scheduler.parse_job_id(submitted.stdout)
    except (SubmissionError, sched.SchedulerError, ssh.SSHError) as exc:
        base.update(started=False, error=str(exc))
        return base

    base.update(
        started=True,
        job_id=dry_job,
        script=dry_plan.remote_script_path,
        stdout=f"{case_dir.rstrip('/')}/pbs-{dry_job}.out",
        stderr=f"{case_dir.rstrip('/')}/pbs-{dry_job}.err",
        n_ranks=plan.resources.get("total_ranks"),
        queue=dry_plan.resources.get("partition"),
        nodes=settings["nodes"],
        submitted_at=_now().isoformat(),
        report=None,
    )
    return base


def maybe_capture_parallel_dryrun(
    machine: str, simulation_id: str, run_id: str
) -> dict[str, Any] | None:
    """Attach the parallel dryrun's verdict to its run, once it has finished.

    Called from status checks and refresh, like ``maybe_capture_build``. A
    rejection cannot block a job that is already queued, so it is reported as
    a warning for the human to act on (cancel and fix) rather than acted upon.
    """
    try:
        record = catalog.read_record(simulation_id)
    except catalog.SimulationCatalogError:
        return None
    run_entry = next(
        (
            r
            for r in record.get("runs") or []
            if isinstance(r, dict) and r.get("run_id") == run_id
        ),
        None,
    )
    if run_entry is None:
        return None
    parallel = run_entry.get("dryrun_parallel")
    if (
        not isinstance(parallel, dict)
        or not parallel.get("started")
        or parallel.get("report")
    ):
        return None

    cfg = m.get_machine(machine)
    scheduler = sched.get_scheduler(cfg)
    host = ssh.host_for(cfg)
    try:
        probe = ssh.run(host, scheduler.status_argv(str(parallel["job_id"])))
        state = scheduler.parse_status(probe.stdout, probe.returncode)
        if state not in sched.TERMINAL_STATES:
            return {
                "captured": False,
                "reason": f"dryrun job {parallel['job_id']} is {state}",
                "retry": True,
            }
        exit_code = scheduler.parse_exit_code(probe.stdout)
        out = ssh.run(host, ["cat", parallel["stdout"]])
        err = ssh.run(host, ["cat", parallel["stderr"]]) if parallel.get("stderr") else None
    except ssh.SSHError as exc:
        return {"captured": False, "reason": str(exc), "retry": True}
    if not out.ok:
        return {"captured": False, "reason": f"cannot read {parallel['stdout']}", "retry": True}
    try:
        rc = int(exit_code) if exit_code is not None else 0
    except ValueError:
        rc = 1
    text = out.stdout + ("\n" + err.stdout if err is not None and err.ok else "")
    report = dryrun.parse_dryrun_output(text, rc)
    payload = report.to_dict()
    payload["job_state"] = state
    payload["checked_at"] = _now().isoformat()
    parallel = dict(parallel)
    parallel["report"] = payload
    try:
        catalog.set_run_field(simulation_id, run_id, "dryrun_parallel", parallel)
    except catalog.SimulationCatalogError as exc:
        return {"captured": False, "reason": str(exc)}
    _store_provenance_field(run_id, "dryrun_parallel", parallel)
    result: dict[str, Any] = {"captured": True, "summary": report.summary(), "ok": report.ok}
    if report.blocking:
        result["warning"] = (
            f"the parallel dryrun REJECTED this input ({report.summary()}); the production "
            f"job {run_entry.get('job_id')} will fail or has failed on the same input -- "
            "consider cancelling it and fixing the input before resubmitting"
        )
    return result


def _store_provenance_field(run_id: str, key: str, value: Any) -> None:
    """Add one field to a run's provenance.json, when the record exists locally."""
    try:
        found = prov.find_record(RUNS_DIR, run_id)
    except prov.ProvenanceError:
        return
    if not found:
        return
    _cls, path = found
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    record[key] = value
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


# Aurora's PBS server has taken over two minutes to answer a single qsub
# (n660au, 2026-09-15): the job was accepted but ssh.run's 120 s default had
# already expired, so the submission returned an error and no provenance was
# written for a job that was really in the queue. Submission is the one call
# where a timeout costs a real, untracked job, so give it its own budget.
SUBMIT_TIMEOUT_SECONDS = 300.0


def submit(
    machine: str,
    *,
    confirm: bool = False,
    plan: SubmissionPlan | None = None,
    case_dir: str | None = None,
    skip_dryrun: bool | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Submit a job, but only with explicit confirmation.

    Without confirm=True this returns the plan with submitted=False and touches
    nothing remote. That is the human-in-the-loop gate from absolute rule 1.

    When case_dir is given, `xgc --dryrun` runs on the login node first and a
    rejected input blocks submission. It costs seconds and no allocation, so it
    is far cheaper than discovering a bad namelist after queueing. skip_dryrun
    forces past it; a caller doing that should have a reason.

    ``skip_dryrun=None`` (the default) decides by the simulation's series: a
    dev (``d``) simulation skips the dryrun, since those are short tests where
    a bad input costs one debug job; production and unregistered runs keep
    it. Pass ``False`` to validate a dev run anyway.

    On machines whose login nodes cannot run the dryrun (``dryrun.login_node:
    false`` -- Polaris, Aurora) the job is submitted first; if it is still
    queued after a short grace period a 1-node job runs the dryrun in parallel
    -- in the first configured queue that can take it now, so a debug queue the
    user already occupies is skipped for a regular one -- and its verdict is
    attached on the next status check.
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

    cfg = m.get_machine(plan.machine)
    settings = dryrun_settings(cfg)
    dryrun_policy = "requested skip" if skip_dryrun else "validated"
    if skip_dryrun is None:
        skip_dryrun = _is_dev_simulation(plan.simulation)
        dryrun_policy = "skipped: dev simulation default" if skip_dryrun else "validated"
    if not case_dir:
        dryrun_policy = "no case_dir"
    dryrun_report: dict[str, Any] | None = None
    parallel_dryrun: dict[str, Any] | None = None
    if case_dir and not skip_dryrun and not settings["login_node"]:
        # No login-node dryrun on this machine: submit first, validate beside
        # the job if it waits in the queue (_start_parallel_dryrun).
        parallel_dryrun = {"mode": "parallel", "started": False, "pending": True}
    elif case_dir and not skip_dryrun:
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

    if case_dir:
        problem = _input_file_dir_problem(
            plan, case_dir, require_absolute=bool(plan.resources.get("daos"))
        )
        if problem:
            return {
                "submitted": False,
                "reason": f"input file dir check failed: {problem}",
                "plan": plan.to_dict(),
            }

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
        timeout=SUBMIT_TIMEOUT_SECONDS,
    )
    job_id = scheduler.parse_job_id(result.stdout)

    if parallel_dryrun is not None and case_dir:
        parallel_dryrun = _start_parallel_dryrun(
            plan, cfg, scheduler, host, job_id, case_dir, settings
        )

    captured = _capture_input(plan.machine, case_dir)
    input_provenance = _input_provenance(
        plan.machine, case_dir, dryrun_report=dryrun_report
    )
    reproduction = _reproduction_provenance(
        plan.machine, case_dir, captured["input_text"], plan.resources
    )
    input_backup = _backup_input(plan.simulation, captured["input_text"])
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
        "referenced_inputs": reproduction["referenced_inputs"],
        "build_environment": reproduction["build_environment"],
        "input_backup": input_backup,
        "dryrun": dryrun_report,
        "dryrun_policy": dryrun_policy,
        "dryrun_parallel": parallel_dryrun,
    }
    if reproduction.get("error"):
        record["reproduction_error"] = reproduction["error"]
    meta_path = _write_provenance(plan, record, input_text=captured["input_text"])
    catalog_link = _link_catalog(record, meta_path)
    notebook = _sync_notebook() if catalog_link else None

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
        "referenced_inputs": reproduction["referenced_inputs"],
        "input_backup": input_backup,
        "notebook": notebook,
        "dryrun": dryrun_report,
        "dryrun_policy": dryrun_policy,
        "dryrun_parallel": parallel_dryrun,
    }


def _is_dev_simulation(simulation: dict[str, Any] | None) -> bool:
    """True for a reserved dev (``d``-series) simulation."""
    if not simulation:
        return False
    if simulation.get("kind"):
        return str(simulation["kind"]) == "dev"
    return catalog.record_kind(str(simulation.get("simulation_id") or "")) == "dev"


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
    linked = _find_run_for_job(machine, job_id)
    if linked:
        dry = maybe_capture_parallel_dryrun(machine, *linked)
        if dry is not None:
            out["dryrun_parallel"] = dry
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
