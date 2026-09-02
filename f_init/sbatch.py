"""Read and rewrite the hand-written Slurm wrapper used by the f_init runs.

The XGCa case directories carry their own ``xgcs.cpu`` (``#SBATCH`` header
plus ``./runscript.cpu``) rather than a script rendered from
``templates/``. Staging a dt=0 directory copies that wrapper, so this module
does the two edits that copy needs -- a new job name and no stale
``--dependency`` -- and reads the header back for the node-hour estimate that
absolute rule 4 requires before submission.
"""

from __future__ import annotations

import re
from typing import Any

_DIRECTIVE = re.compile(r"^#SBATCH\s+--([A-Za-z][A-Za-z0-9-]*)(?:=|\s+)(\S+)")


def parse_directives(text: str) -> dict[str, str]:
    """Active ``#SBATCH`` directives as ``{name: value}``.

    Lines commented out with ``##SBATCH`` are ignored, matching sbatch.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _DIRECTIVE.match(line.strip())
        if match:
            found[match.group(1).lower()] = match.group(2)
    return found


def rewrite(text: str, job_name: str) -> tuple[str, list[str]]:
    """Set the job name and drop dependency directives. Returns (text, notes)."""
    notes: list[str] = []
    out: list[str] = []
    named = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if re.match(r"^#SBATCH\s+--dependency", stripped):
            notes.append(f"dropped stale directive: {stripped}")
            continue
        match = re.match(r"^(#SBATCH\s+--job-name)(=|\s+)(\S+)(.*)$", stripped)
        if match:
            out.append(f"{match.group(1)}={job_name}{match.group(4)}\n")
            notes.append(f"job-name: {match.group(3)} -> {job_name}")
            named = True
            continue
        out.append(line)
    if not named:
        # Insert after the shebang so sbatch still sees it in the header.
        insert_at = 1 if out and out[0].startswith("#!") else 0
        out.insert(insert_at, f"#SBATCH --job-name={job_name}\n")
        notes.append(f"job-name: (absent) -> {job_name}")
    return "".join(out), notes


def walltime_hours(value: str) -> float:
    """Slurm time formats: D-HH:MM:SS, D-HH:MM, D-HH, HH:MM:SS, MM:SS, MM."""
    days = 0
    rest = value.strip()
    if "-" in rest:
        day_text, rest = rest.split("-", 1)
        days = int(day_text)
        parts = [int(p) for p in rest.split(":")]
        while len(parts) < 3:
            parts.append(0)
        hours, minutes, seconds = parts[:3]
    else:
        parts = [int(p) for p in rest.split(":")]
        if len(parts) == 1:
            hours, minutes, seconds = 0, parts[0], 0
        elif len(parts) == 2:
            hours, minutes, seconds = 0, parts[0], parts[1]
        else:
            hours, minutes, seconds = parts[:3]
    return days * 24 + hours + minutes / 60 + seconds / 3600


def resources(text: str) -> dict[str, Any]:
    """Nodes, wall time, and node-hours implied by the ``#SBATCH`` header."""
    directives = parse_directives(text)
    nodes = int(directives.get("nodes", "1"))
    walltime = directives.get("time")
    hours = walltime_hours(walltime) if walltime else None
    return {
        "nodes": nodes,
        "walltime": walltime,
        "qos": directives.get("qos"),
        "account": directives.get("account"),
        "constraint": directives.get("constraint"),
        "job_name": directives.get("job-name"),
        "node_hours": round(nodes * hours, 3) if hours is not None else None,
    }


def parse_runscript(text: str) -> dict[str, Any]:
    """Executable and environment lines from the site ``runscript``.

    Returns ``module`` lines (in order), the ``XGC_BIN`` value, and the
    per-node rank and thread counts when they are literal integers.
    """
    modules: list[str] = []
    info: dict[str, Any] = {"modules": modules, "xgc_bin": None,
                            "ranks_per_node": None, "omp_threads": None}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("module "):
            modules.append(line)
            continue
        match = re.match(r"^export\s+XGC_BIN=(\S+)", line)
        if match:
            info["xgc_bin"] = match.group(1).strip("'\"")
            continue
        match = re.match(r"^export\s+n_mpi_ranks_per_node=(\d+)", line)
        if match:
            info["ranks_per_node"] = int(match.group(1))
            continue
        match = re.match(r"^export\s+OMP_NUM_THREADS=(\d+)", line)
        if match:
            info["omp_threads"] = int(match.group(1))
    return info
