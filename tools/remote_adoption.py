"""Adopt a run that was submitted by hand, from its remote directory.

``tools.run_adoption`` adopts runs that left provenance under ``runs/``. A run
submitted with a hand-written ``qsub`` left nothing local at all -- only the
remote directory and the scheduler's memory of its jobs. This module reads
both and writes the same kind of ``origin: adopted`` record, plus the
reproduction evidence a tool-driven submission would have captured:

* the input (hashed, parsed, and appended to ``input_backup/<directory>``);
* every job the directory's stdout files name, with the scheduler's account
  of each (state, exit code, nodes, wall time, submit time);
* the XGC startup banner from each job's stdout (commit, dirty tree, build);
* the job script, copied under ``runs/adopted/<record_id>/``;
* hashes of the mesh/equilibrium/profile files the input references, and the
  build environment (``hpc_runner._reproduction_provenance``).

It never assigns a sequence number -- the number in a hand-chosen directory
name was consumed outside the registry -- and never invents an outcome beyond
what the scheduler reported. Reading is done with plain ``ls``/``cat``/
``head``/``qstat``/``sacct``; nothing remote is modified.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import build_banner
from tools import hpc_runner
from tools import machines as m
from tools import namelist as nlm
from tools import scheduler as sched
from tools import simulation_catalog as catalog
from tools import ssh

from tools import paths
from tools import catalog_sync

PROJECT_ROOT = paths.PROJECT_ROOT
ADOPTED_DIR = paths.ADOPTED_DIR  # follows XGC_CATALOG_DIR (tools/paths.py)

# stdout files a job leaves behind: XGC's own redirected stdout (the
# hand-written scripts do ``> xgc.out.$PBS_JOBID``), PBS ``<name>.o<id>``, and
# Slurm ``slurm-<id>.out``. The XGC one is preferred for the banner because
# the scheduler file may hold only the script's own echo lines.
_STDOUT_PATTERNS = (
    re.compile(r"^xgc\.out\.(\d+)$"),
    re.compile(r"\.o(\d+)$"),
    re.compile(r"^slurm-(\d+)\.out$"),
)
_JOB_SCRIPT_RE = re.compile(r"\.(pbs|sbatch|sh)$")
_SECTION_RE = re.compile(r"^### (\w+)$")

# PBS Exit_status values that are not XGC's own return code.
_PBS_WALLTIME_EXIT = "-29"     # JOB_EXEC_KILL_WALLTIME
_SIGTERM_EXITS = {"143", "-15"}  # qdel / SIGTERM; walltime kills set -29 instead


class RemoteAdoptionError(RuntimeError):
    """The remote directory cannot be adopted safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _listing_script(remote_dir: str) -> str:
    """One remote command that describes the directory in labelled sections."""
    d = shlex.quote(remote_dir)
    return "\n".join(
        [
            f"cd {d} || {{ echo '### MISSING'; exit 0; }}",
            "echo '### SCRIPTS'; ls -1 *.pbs *.sbatch *.sh 2>/dev/null",
            "echo '### STDOUT'; ls -1 2>/dev/null | grep -E '^xgc\\.out\\.[0-9]+$|\\.o[0-9]+$|^slurm-[0-9]+\\.out$'",
            "for sub in */; do ls -1 \"$sub\" 2>/dev/null | grep -E '^xgc\\.out\\.[0-9]+$|\\.o[0-9]+$|^slurm-[0-9]+\\.out$' | sed \"s#^#$sub#\"; done",
            # timestep.dat has no trailing newline; the echo keeps the next
            # marker on its own line.
            "echo '### TIMESTEP'; [ -f timestep.dat ] && head -c 200 timestep.dat; echo",
            "echo '### LAST3D'; ls -d xgc.3d.*.bp 2>/dev/null | tail -1",
            "echo '### RESTART'; ls -1 restart_dir 2>/dev/null | tail -1",
            "echo '### END'",
        ]
    )


def _split_sections(stdout: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def job_ids_from_stdout_names(names: list[str]) -> dict[str, list[str]]:
    """Map job number -> stdout files that belong to it, XGC's own file first."""
    jobs: dict[str, list[str]] = {}
    for name in names:
        base = name.rsplit("/", 1)[-1]
        for pattern in _STDOUT_PATTERNS:
            match = pattern.search(base)
            if match:
                jobs.setdefault(match.group(1), []).append(name)
                break
    for files in jobs.values():
        files.sort(key=lambda n: (not n.rsplit("/", 1)[-1].startswith("xgc.out."), n))
    return jobs


def parse_qstat_full(stdout: str) -> dict[str, str]:
    """Flatten ``qstat -x -f`` output into ``{key: value}``.

    PBS wraps long values onto continuation lines that start with a tab; those
    are glued back onto the previous key.
    """
    fields: dict[str, str] = {}
    key: str | None = None
    for raw in stdout.splitlines():
        if raw.startswith("\t") and key:
            fields[key] += raw.strip()
            continue
        match = re.match(r"^\s{4}([A-Za-z_.]+) = (.*)$", raw)
        if match:
            key = match.group(1)
            fields[key] = match.group(2).strip()
    return fields


def _pbs_time(value: str | None) -> str | None:
    """``Sat Sep  5 03:14:32 2026`` -> ``2026-09-05T03:14:32`` (server local time)."""
    if not value:
        return None
    try:
        return datetime.strptime(" ".join(value.split()), "%a %b %d %H:%M:%S %Y").isoformat()
    except ValueError:
        return value


def pbs_run_from_fields(fields: dict[str, str], job: str, server_suffix: str) -> dict[str, Any]:
    """Turn one job's qstat fields into a catalog run entry.

    State follows the scheduler's word, not a guess about the science:
    ``Exit_status`` 0 is ``completed``; PBS's walltime kill (-29) is
    ``timeout``; a SIGTERM exit (143) is ``cancelled``, since ``qdel`` sends
    SIGTERM and a walltime kill would have said -29; anything else nonzero is
    ``failed``; a job still queued or running keeps its live state.
    """
    exit_status = fields.get("Exit_status")
    raw_state = (fields.get("job_state") or "").upper()
    comment = fields.get("comment", "")
    if raw_state and raw_state not in {"F", "X", "C"}:
        state = sched._PBS_STATES.get(raw_state, sched.UNKNOWN)
    elif exit_status is None:
        state = sched.CANCELLED if re.search(r"terminated|delet|cancel", comment, re.I) else sched.UNKNOWN
    elif exit_status == "0":
        state = sched.COMPLETED
    elif exit_status == _PBS_WALLTIME_EXIT or re.search(r"walltime", comment, re.I):
        state = sched.TIMEOUT
    elif exit_status in _SIGTERM_EXITS or re.search(r"delet|cancel", comment, re.I):
        state = sched.CANCELLED
    else:
        state = sched.FAILED
    try:
        exit_code: int | None = int(exit_status) if exit_status is not None else None
    except ValueError:
        exit_code = None
    entry: dict[str, Any] = {
        "run_id": f"pbs-{job}",
        "job_id": f"{job}{server_suffix}",
        "job_name": fields.get("Job_Name"),
        "submitted_at": _pbs_time(fields.get("qtime")),
        "started_at": _pbs_time(fields.get("stime")),
        "ended_at": _pbs_time(fields.get("obittime")),
        "time_zone": "scheduler server local time",
        "state": state,
        "exit_code": exit_code,
        "nodes": int(fields["Resource_List.nodect"]) if fields.get("Resource_List.nodect", "").isdigit() else None,
        "walltime_limit": fields.get("Resource_List.walltime"),
        "walltime_used": fields.get("resources_used.walltime"),
        "queue": fields.get("queue"),
        "scheduler_source": "qstat -x -f",
    }
    if comment:
        entry["scheduler_comment"] = comment[:200]
    return entry


_SACCT_FORMAT = "JobID,JobName,State,ExitCode,NNodes,Submit,Start,End,Elapsed,Timelimit,Partition"


def slurm_run_from_sacct(stdout: str, job: str) -> dict[str, Any] | None:
    """The job's own sacct row (not .batch/.extern) as a catalog run entry."""
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 11 or parts[0].strip() != job:
            continue
        raw_state = parts[2].strip().split()[0].upper() if parts[2].strip() else ""
        exit_code: int | None = None
        try:
            exit_code = int(parts[3].split(":")[0])
        except ValueError:
            pass
        return {
            "run_id": f"slurm-{job}",
            "job_id": job,
            "job_name": parts[1].strip() or None,
            "submitted_at": parts[5].strip() or None,
            "started_at": parts[6].strip() or None,
            "ended_at": parts[7].strip() or None,
            "time_zone": "scheduler server local time",
            "state": sched._SLURM_STATES.get(raw_state, sched.UNKNOWN),
            "exit_code": exit_code,
            "nodes": int(parts[4]) if parts[4].strip().isdigit() else None,
            "walltime_limit": parts[9].strip() or None,
            "walltime_used": parts[8].strip() or None,
            "queue": parts[10].strip() or None,
            "scheduler_source": "sacct",
        }
    return None


def _script_resources(script_text: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Executable, env script and build dir from a hand-written job script.

    Falls back to the machine's configured build when the script does not say.
    """
    xgc_cfg = cfg.get("xgc") or {}
    executable = None
    env_script = None
    src = None
    for line in script_text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        match = re.match(r"^(?:export\s+)?SRC=(\S+)", stripped)
        if match:
            src = match.group(1).strip("\"'")
        match = re.match(r"^(?:export\s+)?XGC_BIN=(\S+)", stripped)
        if match:
            executable = match.group(1).strip("\"'")
        match = re.search(r"\bsource\s+\"?([^\s\"]+)\"?", stripped)
        if match and not env_script:
            env_script = match.group(1)
    if src:
        if executable:
            executable = executable.replace("${SRC}", src).replace("$SRC", src)
        if env_script:
            env_script = env_script.replace("${SRC}", src).replace("$SRC", src)
    build_dir = None
    if executable and executable.endswith(("/xgc", "/xgca")):
        # <build>/bin/xgc -> <build>
        build_dir = executable.rsplit("/", 2)[0]
    return {
        "executable": executable or xgc_cfg.get("exe"),
        "env_script": env_script or xgc_cfg.get("env_script"),
        "build_dir": build_dir or xgc_cfg.get("build"),
        "xgc_commit": ((xgc_cfg.get("verified") or {}).get("commit")),
        "resources_source": "job script" if executable else "machines config",
    }


def _step_number(name: str | None) -> int | None:
    if not name:
        return None
    match = re.search(r"(\d{4,6})", name)
    return int(match.group(1)) if match else None


def _fetch_input_text(host: ssh.SSHTarget, remote_dir: str) -> tuple[str, str]:
    """The run's namelist: ``input``, or XGC's own echo ``fort.input.used``.

    A hand run whose ``input`` was removed still has the echo of the namelist
    XGC read; it is adopted from that, and the record says so. XGC copies
    comment bytes verbatim, so the echo may not be UTF-8: latin-1 decodes
    every byte and leaves the namelist itself intact.
    """
    base = remote_dir.rstrip("/")
    fetched = ssh.run(host, ["cat", f"{base}/input"], timeout=60)
    if fetched.ok and fetched.stdout.strip():
        return fetched.stdout, "input"
    fetched = ssh.run(
        host, ["bash", "-c", f"iconv -f latin1 -t utf-8 {shlex.quote(base + '/fort.input.used')}"], timeout=60
    )
    return (fetched.stdout if fetched.ok else ""), "fort.input.used"


def inspect_remote_run(machine: str, remote_dir: str) -> dict[str, Any]:
    """Read everything the adoption needs from the machine. Modifies nothing."""
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    scheduler = sched.get_scheduler(cfg)
    listing = ssh.run(host, ["bash", "-c", _listing_script(remote_dir)], timeout=180)
    sections = _split_sections(listing.stdout)
    if "MISSING" in sections or "END" not in sections:
        raise RemoteAdoptionError(
            f"{remote_dir} is not readable on {machine}: {listing.stderr.strip()[-300:]}"
        )
    # The input is fetched on its own so the bytes (and therefore the hash)
    # are exactly the remote file's, not a reconstruction from a listing.
    input_text, input_source = _fetch_input_text(host, remote_dir)
    if not input_text.strip():
        raise RemoteAdoptionError(f"{remote_dir} has no input file; nothing to adopt")
    stdout_names = [n for n in sections.get("STDOUT", []) if n.strip()]
    jobs = job_ids_from_stdout_names(stdout_names)

    # Scheduler history, one query per job. Hand runs are few.
    runs: list[dict[str, Any]] = []
    server_suffix = ""
    for job in sorted(jobs, key=int):
        if scheduler.type_name == "pbs":
            result = ssh.run(host, ["qstat", "-x", "-f", job], timeout=60)
            fields = parse_qstat_full(result.stdout) if result.ok else {}
            if not server_suffix:
                match = re.search(r"^Job Id: \d+(\S*)", result.stdout, re.M)
                server_suffix = match.group(1) if match else ""
            entry = pbs_run_from_fields(fields, job, server_suffix) if fields else {
                "run_id": f"pbs-{job}", "job_id": job, "state": sched.UNKNOWN,
                "exit_code": None, "scheduler_source": "qstat -x -f (job no longer known)",
            }
        else:
            result = ssh.run(
                host, ["sacct", "-j", job, "-n", "-P", "-o", _SACCT_FORMAT], timeout=60
            )
            entry = (slurm_run_from_sacct(result.stdout, job) if result.ok else None) or {
                "run_id": f"slurm-{job}", "job_id": job, "state": sched.UNKNOWN,
                "exit_code": None, "scheduler_source": "sacct (job no longer known)",
            }
        entry["stdout_files"] = jobs[job]
        entry["remote_run_dir"] = remote_dir
        entry["provenance"] = None
        runs.append(entry)

    # Startup banner from each job's stdout, XGC's own file first.
    for entry in runs:
        for name in entry["stdout_files"]:
            head = ssh.run(
                host, ["head", "-n", str(build_banner._SCAN_LINE_LIMIT), f"{remote_dir}/{name}"], timeout=90
            )
            banner = build_banner.parse_build_banner(head.stdout) if head.ok else None
            if banner:
                entry["build"] = {**banner, "source_file": name}
                break

    scripts = [n for n in sections.get("SCRIPTS", []) if n.strip()]
    script_name = None
    script_text = None
    for name in scripts:
        text = ssh.run(host, ["cat", f"{remote_dir}/{name}"], timeout=60)
        if text.ok and re.search(r"^#(PBS|SBATCH) ", text.stdout, re.M):
            script_name, script_text = name, text.stdout
            break
    resources = _script_resources(script_text or "", cfg)
    reproduction = hpc_runner._reproduction_provenance(machine, remote_dir, input_text, resources)

    parsed = nlm.referenced_input_files  # noqa: F841 - documented dependency
    return {
        "machine": machine,
        "remote_dir": remote_dir,
        "directory_name": remote_dir.rstrip("/").rsplit("/", 1)[-1],
        "input_text": input_text,
        "input_source": input_source,
        "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        "runs": runs,
        "job_script": {"name": script_name, "text": script_text} if script_text else None,
        "resources": resources,
        "referenced_inputs": reproduction.get("referenced_inputs"),
        "build_environment": reproduction.get("build_environment"),
        "reproduction_error": reproduction.get("error"),
        "progress": {
            "timestep_dat": " ".join(" ".join(sections.get("TIMESTEP", [])).split()) or None,
            "last_3d_file": next((n for n in sections.get("LAST3D", []) if n.strip()), None),
            "last_restart": next((n for n in sections.get("RESTART", []) if n.strip()), None),
        },
        "inspected_at": _now(),
    }


def _local_input_summary(input_text: str) -> dict[str, Any]:
    """Parse the input locally into the catalog's provenance shape."""
    parsed: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in input_text.splitlines():
        line = raw.split("!", 1)[0].rstrip()
        match = re.match(r"^\s*&([A-Za-z][A-Za-z0-9_]*)", line)
        if match:
            current = match.group(1).lower()
            parsed.setdefault(current, {})
            continue
        if line.strip() == "/":
            current = None
            continue
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)(?:\s*\([^)]*\))?\s*=\s*(.*?)\s*$", line)
        if match and current is not None:
            parsed[current][match.group(1).lower()] = match.group(2)
    try:
        return nlm.provenance_input_summary(parsed)
    except (KeyError, TypeError, ValueError) as exc:
        return {"parse_status": "unavailable", "parse_error": str(exc)[-200:], "parameters": {}, "derived": {}}


def build_record(
    inspection: dict[str, Any],
    *,
    tags: list[str] | None = None,
    case: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Turn an inspection into an ``origin: adopted`` record. Writes nothing."""
    directory_name = inspection["directory_name"]
    record_id = directory_name.lower()
    machine = inspection["machine"]
    record = catalog.new_record(
        record_id,
        simulation_tag=catalog.infer_simulation_tag(directory_name, fallback=record_id),
        sub_tag=directory_name,
        machines=[machine],
        short_name=re.sub(r"^n\d+[a-z]{2}(?:_[a-z]{2})*_?", "", directory_name) or directory_name,
        directory_name=directory_name,
        tags=["adopted", *(tags or [])],
        status="submitted",
        origin="adopted",
        case=case,
        note=note,
    )
    runs = []
    for entry in inspection["runs"]:
        run = dict(entry)
        run["machine"] = machine
        runs.append(run)
    record["runs"] = runs
    record["remote_dirs"] = {machine: inspection["remote_dir"]}
    record["input_sha256"] = inspection["input_sha256"]
    record["input"] = _local_input_summary(inspection["input_text"])
    if inspection.get("referenced_inputs"):
        record["referenced_inputs"] = inspection["referenced_inputs"]
    if inspection.get("build_environment"):
        record["build_environment"] = inspection["build_environment"]
    progress = inspection.get("progress") or {}
    record["progress"] = {
        "last_gstep": _step_number(progress.get("last_3d_file")),
        "last_restart_step": _step_number(progress.get("last_restart")),
        "timestep_dat": progress.get("timestep_dat"),
        "checked_at": inspection["inspected_at"][:10],
    }
    started = sorted(r.get("submitted_at") or "" for r in runs if r.get("submitted_at"))
    record["started_on"] = started[0][:10] if started else None
    builds = [r["build"] for r in runs if isinstance(r.get("build"), dict)]
    commit = next((b.get("git", {}).get("commit") for b in builds if b.get("git", {}).get("commit")), None)
    record["xgc_commit"] = commit or (inspection.get("resources") or {}).get("xgc_commit")
    record["simulation_id"] = None
    record["number"] = None
    number = catalog.record_number(catalog.infer_simulation_tag(directory_name, fallback=record_id))
    record["adoption"] = {
        "source": "remote run directory and scheduler history (hand-submitted, no runs/ provenance)",
        "grouped_by": "working directory",
        "directory": inspection["remote_dir"],
        "provenance_classes": [],
        "numbered": False,
        "number_in_name": number,
        "detail": (
            "adopted from the remote directory; the number in the directory name "
            "was consumed outside the registry and is not a reservation"
        ),
    }
    record["status"] = catalog.derive_status(record)
    return record


def plan_remote_adoption(
    machine: str,
    remote_dir: str,
    *,
    tags: list[str] | None = None,
    case: str | None = None,
    note: str = "",
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect the directory and show the record that would be written."""
    inspection = inspect_remote_run(machine, remote_dir)
    record = build_record(inspection, tags=tags, case=case, note=note)
    existing = catalog.find_record(record["record_id"], catalog_dir=catalog_dir)
    blocked = None
    if existing is not None and existing.get("origin") != "adopted":
        blocked = (
            f"{record['record_id']} is already an {existing.get('origin')} record; "
            "a registered simulation is never overwritten by adoption"
        )
    return {
        "record": record,
        "inspection": {k: v for k, v in inspection.items() if k not in {"input_text"}},
        "existing": existing.get("origin") if existing else None,
        "blocked": blocked,
        "writes": False,
    }


@catalog_sync.synced(lambda result, **k: f"adopt {str(k.get('remote_dir', '')).rstrip('/').rsplit('/', 1)[-1]} ({k.get('machine', '')})")
def adopt_remote(
    machine: str,
    remote_dir: str,
    *,
    confirm: bool = False,
    tags: list[str] | None = None,
    case: str | None = None,
    note: str = "",
    overwrite: bool = False,
    catalog_dir: Path | None = None,
    input_backup_dir: Path | None = None,
    adopted_dir: Path | None = None,
) -> dict[str, Any]:
    """Write the record, the input backup and the supporting files.

    Without ``confirm`` this is ``plan_remote_adoption``. An existing adopted
    record is refreshed only with ``overwrite``; its human note is kept.
    """
    plan = plan_remote_adoption(
        machine, remote_dir, tags=tags, case=case, note=note, catalog_dir=catalog_dir
    )
    if not confirm:
        plan["adopted"] = False
        plan["reason"] = "confirmation required; review the record and resubmit with confirm=True"
        return plan
    if plan["blocked"]:
        raise RemoteAdoptionError(plan["blocked"])
    record = plan["record"]
    record_id = record["record_id"]
    existing = catalog.find_record(record_id, catalog_dir=catalog_dir)
    if existing is not None:
        if not overwrite:
            raise RemoteAdoptionError(
                f"{record_id} already exists as an adopted record; pass overwrite=True to refresh it"
            )
        if existing.get("note") and not note:
            record["note"] = existing["note"]
        record["created_at"] = existing.get("created_at", record["created_at"])
        if existing.get("status") in catalog._HUMAN_JUDGEMENT_STATUSES:
            record["status"] = existing["status"]

    inspection = inspect_cache = plan["inspection"]
    input_text = _input_text_from_plan(machine, remote_dir, inspect_cache)
    backup_dir = input_backup_dir or hpc_runner.INPUT_BACKUP_DIR
    with_dir = hpc_runner.INPUT_BACKUP_DIR
    try:
        hpc_runner.INPUT_BACKUP_DIR = backup_dir
        backup = hpc_runner._backup_input({"directory_name": record["directory_name"]}, input_text)
    finally:
        hpc_runner.INPUT_BACKUP_DIR = with_dir
    record["input_backup"] = backup

    support_dir = (adopted_dir or ADOPTED_DIR) / record_id
    support_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    script = inspection.get("job_script")
    if script:
        path = support_dir / script["name"]
        path.write_text(script["text"], encoding="utf-8")
        record["job_script"] = {
            "remote": f"{remote_dir.rstrip('/')}/{script['name']}",
            "local_copy": _display(path),
            "sha256": hashlib.sha256(script["text"].encode("utf-8")).hexdigest(),
            "size_bytes": len(script["text"].encode("utf-8")),
        }
        written.append(_display(path))
    for run in record["runs"]:
        if isinstance(run.get("build"), dict):
            job = str(run["job_id"]).split(".")[0]
            path = support_dir / f"banner_{job}.json"
            path.write_text(
                json.dumps({"source_file": run["build"].get("source_file"), "banner": run["build"]}, indent=1)
                + "\n",
                encoding="utf-8",
            )
            written.append(_display(path))

    catalog.write_record(record, catalog_dir=catalog_dir)
    return {
        "adopted": True,
        "record_id": record_id,
        "status": record["status"],
        "runs": len(record["runs"]),
        "banners": sum(1 for r in record["runs"] if r.get("build")),
        "input_backup": backup,
        "support_files": written,
        "record_path": _display(catalog.record_path(record_id, catalog_dir=catalog_dir)),
        "writes": True,
    }


def _input_text_from_plan(machine: str, remote_dir: str, inspection: dict[str, Any]) -> str:
    """The plan strips the input text from its report; read it back once."""
    cfg = m.get_machine(machine)
    text, _source = _fetch_input_text(ssh.host_for(cfg), remote_dir)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != inspection.get("input_sha256"):
        raise RemoteAdoptionError(
            "the remote input changed between planning and writing; rerun the adoption"
        )
    return text


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)
