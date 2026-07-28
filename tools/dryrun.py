"""Pre-submission input validation via `xgc --dryrun`.

XGC itself is the authority on whether an input file is valid, so we run the
real binary rather than reimplementing its rules. `--dryrun` parses the
namelist, loads the mesh, predicts memory, and checks particle resolution, then
returns before PETSc/ADIOS2 initialization.

It is cheap enough to run on a login node: ~3 s for the ecbc case, single rank,
no allocation consumed. That makes it a practical gate to run before every
submission (AGENTS.md, absolute rule 4: know the cost before spending it).

Exit codes:
    0  input accepted (warnings may still be present)
    1  input rejected -- typically a removed parameter

`-n_ranks N` tells the dryrun the rank count you intend to use, so its memory
and resolution estimates match the job you are about to submit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tools import machines as m
from tools import ssh

# "  Input parameter: sml_foo" followed by "     Suggestion: ..."
_PARAM_RE = re.compile(r"^\s*Input parameter:\s*(\S+)\s*$")
_SUGGESTION_RE = re.compile(r"^\s*Suggestion:\s*(.+?)\s*$")

_DEPRECATED_HEADER = "WARNING: Deprecated input detected"
_REMOVED_HEADER = "ERROR: Removed input detected"

# "	TOTAL usage per rank: 0.146 GB"
_MEM_TOTAL_RE = re.compile(r"TOTAL usage per rank:\s*([\d.]+)\s*GB")
_MEM_AVAIL_RE = re.compile(r"([\d.]+)\s*GB per rank is available on host")
# "Particles per 5D mesh vertex: 11375616 ptl / 220231616 vertices = 0.051653"
_RESOLUTION_RE = re.compile(
    r"Particles per 5D mesh vertex:\s*(\d+)\s*ptl\s*/\s*(\d+)\s*vertices\s*=\s*([\d.eE+-]+)"
)
_RANKS_RE = re.compile(r"Number of MPI ranks:\s*(\d+)")
_THREADS_RE = re.compile(r"Number of OpenMP threads per MPI rank:\s*(\d+)")
_FIELD_MODEL_RE = re.compile(r"Field model:\s*(\S+)")


@dataclass
class InputIssue:
    """A single deprecated or removed input parameter."""

    parameter: str
    suggestion: str
    severity: str  # "deprecated" | "removed"

    def to_dict(self) -> dict[str, str]:
        return {
            "parameter": self.parameter,
            "suggestion": self.suggestion,
            "severity": self.severity,
        }


@dataclass
class DryrunReport:
    """Parsed result of `xgc --dryrun`."""

    ok: bool
    returncode: int
    removed: list[InputIssue] = field(default_factory=list)
    deprecated: list[InputIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    resolution: dict[str, Any] = field(default_factory=dict)
    parallelism: dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    raw_tail: str = ""

    @property
    def blocking(self) -> bool:
        """Whether this result must prevent submission."""
        return not self.ok

    def summary(self) -> str:
        """One-line verdict for a human."""
        if self.removed:
            names = ", ".join(i.parameter for i in self.removed)
            return f"REJECTED: {len(self.removed)} removed parameter(s): {names}"
        if not self.ok:
            return f"REJECTED: xgc --dryrun exited {self.returncode}"
        bits = []
        if self.deprecated:
            bits.append(f"{len(self.deprecated)} deprecated parameter(s)")
        if self.warnings:
            bits.append(f"{len(self.warnings)} warning(s)")
        return "ACCEPTED" + (f" with {', '.join(bits)}" if bits else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocking": self.blocking,
            "summary": self.summary(),
            "returncode": self.returncode,
            "completed": self.completed,
            "removed": [i.to_dict() for i in self.removed],
            "deprecated": [i.to_dict() for i in self.deprecated],
            "warnings": self.warnings,
            "memory": self.memory,
            "resolution": self.resolution,
            "parallelism": self.parallelism,
            "raw_tail": self.raw_tail,
        }


def parse_dryrun_output(text: str, returncode: int) -> DryrunReport:
    """Parse dryrun stdout into a structured report.

    Kept separate from execution so it can be unit-tested on captured output
    without needing a cluster.
    """
    report = DryrunReport(ok=(returncode == 0), returncode=returncode)

    # Parameter issues appear under a section header; track which one we are in.
    section: str | None = None
    pending: str | None = None

    for line in text.splitlines():
        if _DEPRECATED_HEADER in line:
            section, pending = "deprecated", None
            continue
        if _REMOVED_HEADER in line:
            section, pending = "removed", None
            continue

        param = _PARAM_RE.match(line)
        if param:
            pending = param.group(1)
            continue

        suggestion = _SUGGESTION_RE.match(line)
        if suggestion and pending and section:
            issue = InputIssue(pending, suggestion.group(1), section)
            (report.removed if section == "removed" else report.deprecated).append(issue)
            pending = None
            continue

        # Resolution warnings are free-form and worth surfacing verbatim.
        stripped = line.strip()
        if stripped.startswith("Warning:"):
            report.warnings.append(stripped[len("Warning:"):].strip())

        if "Dryrun completed" in line:
            report.completed = True

    mem_total = _MEM_TOTAL_RE.search(text)
    mem_avail = _MEM_AVAIL_RE.search(text)
    if mem_total:
        report.memory["per_rank_gb"] = float(mem_total.group(1))
    if mem_avail:
        report.memory["available_per_rank_gb"] = float(mem_avail.group(1))
    if mem_total and mem_avail:
        avail = float(mem_avail.group(1))
        report.memory["headroom_ratio"] = (
            round(float(mem_total.group(1)) / avail, 4) if avail else None
        )

    res = _RESOLUTION_RE.search(text)
    if res:
        report.resolution = {
            "particles": int(res.group(1)),
            "vertices_5d": int(res.group(2)),
            "particles_per_vertex": float(res.group(3)),
        }

    ranks = _RANKS_RE.search(text)
    threads = _THREADS_RE.search(text)
    model = _FIELD_MODEL_RE.search(text)
    if ranks:
        report.parallelism["mpi_ranks"] = int(ranks.group(1))
    if threads:
        report.parallelism["omp_threads_per_rank"] = int(threads.group(1))
    if model:
        report.parallelism["field_model"] = model.group(1)

    # Keep a bounded tail for cases the parser does not recognize.
    report.raw_tail = "\n".join(text.splitlines()[-40:])
    return report


def run_dryrun(
    machine: str,
    remote_case_dir: str,
    *,
    n_ranks: int | None = None,
    xgc_exe: str | None = None,
    timeout: float = 600.0,
) -> DryrunReport:
    """Run `xgc --dryrun` in a remote case directory on the login node.

    Args:
        machine: machine key from machines.yaml.
        remote_case_dir: directory holding `input`, `input_dir/`, etc.
        n_ranks: rank count the real job will use, so estimates match.
        xgc_exe: override the executable from machines.yaml.

    Runs on the login node: single rank, seconds, no scheduler involvement.
    """
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    xgc_cfg = cfg.get("xgc", {})

    exe = xgc_exe or xgc_cfg.get("exe")
    if not exe or m.is_placeholder(exe):
        raise ValueError(
            f"no usable XGC executable for {machine!r} (got {exe!r}). "
            f"Set xgc.exe in machines.yaml."
        )

    if not ssh.exists(host, f"{remote_case_dir}/input"):
        raise FileNotFoundError(f"no 'input' file in {remote_case_dir} on {machine}")

    argv = [exe, "--dryrun"]
    if n_ranks is not None:
        argv += ["-n_ranks", str(n_ranks)]

    result = ssh.run(
        host,
        argv,
        cwd=remote_case_dir,
        env_script=xgc_cfg.get("env_script"),
        timeout=timeout,
    )
    # XGC writes diagnostics to stdout, but MPI_ABORT messaging goes to stderr;
    # parse both so a rejection is never missed.
    return parse_dryrun_output(result.stdout + "\n" + result.stderr, result.returncode)
