"""Scheduler adapters (slurm / pbs).

All scheduler-specific knowledge lives here: command construction, job-id
parsing, and state-code mapping. Everything above this layer works with the
normalized JobState vocabulary and never branches on scheduler type
(AGENTS.md: higher-level logic must remain unaware of the machine type).
"""

from __future__ import annotations

import re
from typing import Any

# Normalized job states. Scheduler-specific codes map onto these.
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED})

# squeue/sacct state codes -> normalized. Slurm reports both long names and
# 2-letter codes depending on format, so both are listed.
_SLURM_STATES = {
    "PD": PENDING, "PENDING": PENDING,
    "CF": PENDING, "CONFIGURING": PENDING,
    "R": RUNNING, "RUNNING": RUNNING,
    "CG": RUNNING, "COMPLETING": RUNNING,
    "CD": COMPLETED, "COMPLETED": COMPLETED,
    "F": FAILED, "FAILED": FAILED,
    "TO": FAILED, "TIMEOUT": FAILED,
    "NF": FAILED, "NODE_FAIL": FAILED,
    "OOM": FAILED, "OUT_OF_MEMORY": FAILED,
    "CA": CANCELLED, "CANCELLED": CANCELLED,
}

# qstat state codes -> normalized.
_PBS_STATES = {
    "Q": PENDING, "H": PENDING, "W": PENDING, "T": PENDING,
    "R": RUNNING, "E": RUNNING,
    "F": COMPLETED, "C": COMPLETED,
    "X": COMPLETED,
}


class SchedulerError(RuntimeError):
    """Scheduler interaction failed in a way the caller cannot recover from."""


class Scheduler:
    """Base adapter. Subclasses implement one scheduler each."""

    type_name: str = ""

    def submit_argv(self, script_path: str) -> list[str]:
        raise NotImplementedError

    def parse_job_id(self, stdout: str) -> str:
        raise NotImplementedError

    def status_argv(self, job_id: str) -> list[str]:
        raise NotImplementedError

    def parse_status(self, stdout: str, returncode: int) -> str:
        raise NotImplementedError

    def cancel_argv(self, job_id: str) -> list[str]:
        raise NotImplementedError


class SlurmScheduler(Scheduler):
    type_name = "slurm"

    def submit_argv(self, script_path: str) -> list[str]:
        return ["sbatch", script_path]

    def parse_job_id(self, stdout: str) -> str:
        # "Submitted batch job 12345"
        match = re.search(r"Submitted batch job (\d+)", stdout)
        if not match:
            raise SchedulerError(f"could not parse job id from sbatch output: {stdout!r}")
        return match.group(1)

    def status_argv(self, job_id: str) -> list[str]:
        # -h suppresses the header; %T gives the long state name.
        return ["squeue", "-j", job_id, "-h", "-o", "%T"]

    def parse_status(self, stdout: str, returncode: int) -> str:
        text = stdout.strip()
        if text:
            return _SLURM_STATES.get(text.split()[0].upper(), UNKNOWN)
        # squeue forgets jobs shortly after they finish. An empty result means
        # the job left the queue, not that it never existed; the caller should
        # consult sacct for the real outcome.
        return UNKNOWN

    def cancel_argv(self, job_id: str) -> list[str]:
        return ["scancel", job_id]

    def accounting_argv(self, job_id: str) -> list[str]:
        """Query sacct for a finished job's state and exit code."""
        return ["sacct", "-j", job_id, "-n", "-P", "-o", "State,ExitCode"]

    def parse_accounting(self, stdout: str) -> tuple[str, str | None]:
        """Parse sacct output into (normalized state, exit code).

        Only the first line matters: sacct also lists .batch/.extern job steps.
        """
        for line in stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            raw_state = parts[0].strip().split()[0].upper()
            exit_code = parts[1].strip() if len(parts) > 1 else None
            return _SLURM_STATES.get(raw_state, UNKNOWN), exit_code
        return UNKNOWN, None


class PBSScheduler(Scheduler):
    type_name = "pbs"

    def submit_argv(self, script_path: str) -> list[str]:
        return ["qsub", script_path]

    def parse_job_id(self, stdout: str) -> str:
        # qsub echoes the job id, e.g. "12345.polaris-pbs-01"
        text = stdout.strip()
        if not text:
            raise SchedulerError("qsub produced no output")
        return text.splitlines()[-1].strip()

    def status_argv(self, job_id: str) -> list[str]:
        return ["qstat", "-x", "-f", job_id]

    def parse_status(self, stdout: str, returncode: int) -> str:
        match = re.search(r"job_state\s*=\s*(\w)", stdout)
        if not match:
            return UNKNOWN
        return _PBS_STATES.get(match.group(1).upper(), UNKNOWN)

    def cancel_argv(self, job_id: str) -> list[str]:
        return ["qdel", job_id]


_ADAPTERS: dict[str, type[Scheduler]] = {
    "slurm": SlurmScheduler,
    "pbs": PBSScheduler,
}


def get_scheduler(machine_cfg: dict[str, Any]) -> Scheduler:
    """Build the adapter for a machine definition.

    Raises if the scheduler type is missing or still a `<...>` placeholder,
    which is the case for machines we have not confirmed yet. Guessing here
    would mean submitting with the wrong command.
    """
    sched_type = machine_cfg.get("scheduler", {}).get("type")
    if not sched_type:
        raise SchedulerError("machine definition has no scheduler.type")
    if isinstance(sched_type, str) and sched_type.startswith("<"):
        raise SchedulerError(
            f"scheduler.type is still a placeholder ({sched_type!r}). "
            f"Confirm it in machines.yaml before submitting."
        )
    adapter = _ADAPTERS.get(sched_type.lower())
    if adapter is None:
        raise SchedulerError(
            f"unsupported scheduler type {sched_type!r}; known: {sorted(_ADAPTERS)}"
        )
    return adapter()
