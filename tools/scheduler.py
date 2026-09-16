"""Scheduler adapters (slurm / pbs).

All scheduler-specific knowledge lives here: command construction, job-id
parsing, and state-code mapping. Everything above this layer works with the
normalized JobState vocabulary and never branches on scheduler type
(AGENTS.md: higher-level logic must remain unaware of the machine type).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Sequence

# Normalized job states. Scheduler-specific codes map onto these.
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
# Hitting the wall clock is its own outcome, not a failure. A long XGC run is
# routinely sized to run out of time and restart from its checkpoint, so
# folding TIMEOUT into FAILED would report normal operation as an error.
# Whether the science finished is a separate question from how the job ended.
TIMEOUT = "timeout"
UNKNOWN = "unknown"

TERMINAL_STATES = frozenset({COMPLETED, FAILED, CANCELLED, TIMEOUT})


def _start_time(value: Any) -> str | None:
    """Normalize scheduler timestamps to ISO UTC. Query commands force UTC/C.

    PBS JSON may encode resource timestamps as epoch numbers or ctime strings;
    Slurm %S uses an ISO timestamp. Unset/invalid values are not estimates.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            if int(text) <= 0:
                return None
            parsed = datetime.fromtimestamp(int(text), timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %Y")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None

# squeue/sacct state codes -> normalized. Slurm reports both long names and
# 2-letter codes depending on format, so both are listed.
_SLURM_STATES = {
    "PD": PENDING, "PENDING": PENDING,
    "CF": PENDING, "CONFIGURING": PENDING,
    "R": RUNNING, "RUNNING": RUNNING,
    "CG": RUNNING, "COMPLETING": RUNNING,
    "CD": COMPLETED, "COMPLETED": COMPLETED,
    "F": FAILED, "FAILED": FAILED,
    "TO": TIMEOUT, "TIMEOUT": TIMEOUT,
    "NF": FAILED, "NODE_FAIL": FAILED,
    "OOM": FAILED, "OUT_OF_MEMORY": FAILED,
    "CA": CANCELLED, "CANCELLED": CANCELLED,
}

# qstat state codes -> normalized.
_PBS_STATES = {
    "Q": PENDING, "H": PENDING, "W": PENDING, "T": PENDING,
    "R": RUNNING, "E": RUNNING, "B": RUNNING, "S": RUNNING,
    "F": COMPLETED, "C": COMPLETED,
    "X": COMPLETED,
}


class SchedulerError(RuntimeError):
    """Scheduler interaction failed in a way the caller cannot recover from."""


class Scheduler:
    """Base adapter. Subclasses implement one scheduler each."""

    type_name: str = ""

    # Whether this scheduler can answer about many jobs in one command. Every
    # remote call costs seconds of login-shell startup, so bulk polling is the
    # difference between seconds and minutes when checking a whole catalog.
    # Adapters that cannot do it leave this False and callers fall back to
    # one query per job.
    supports_bulk_status: bool = False

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

    def user_queue_argv(self, user: str) -> list[str]:
        """List every queued job owned by ``user``, job ids not known upfront.

        ``status_argv`` answers about ids the catalog already holds. This walks
        the other way, which is what a watcher needs: jobs submitted by hand,
        outside the automation, have no catalog record to look them up from.
        """
        raise NotImplementedError(
            f"the {self.type_name or 'configured'} adapter cannot list a "
            "user's queue yet"
        )

    def parse_user_queue(self, stdout: str) -> list[dict[str, Any]]:
        """Parse ``user_queue_argv`` output into normalized job dicts."""
        raise NotImplementedError(
            f"the {self.type_name or 'configured'} adapter cannot list a "
            "user's queue yet"
        )

    def user_queue_detail_argv(self, job_ids: Sequence[str]) -> list[str] | None:
        """Optional second round trip that completes the phase-one job dicts.

        Some schedulers describe a whole user queue in one command; others can
        only be asked "which ids are mine?" first and "what are they doing?"
        second. Returning ``None`` means phase one was already complete, so a
        caller never needs to know which kind of scheduler it is talking to.
        """
        return None

    def parse_user_queue_detail(self, stdout: str) -> dict[str, dict[str, Any]]:
        """Map job id -> fields to merge onto the phase-one job dicts."""
        return {}

    def accounting_argv(self, job_id: str) -> list[str]:
        """Query how a job that has left the queue actually ended."""
        raise NotImplementedError(
            f"the {self.type_name or 'configured'} adapter cannot look up a "
            "finished job yet"
        )

    def parse_accounting(self, stdout: str) -> tuple[str, str | None]:
        """Parse ``accounting_argv`` output into (normalized state, exit code)."""
        raise NotImplementedError(
            f"the {self.type_name or 'configured'} adapter cannot look up a "
            "finished job yet"
        )

    def parse_exit_code(self, stdout: str) -> str | None:
        """Return a scheduler exit code embedded in status output, if any."""
        return None

    def validate_resources(self, resources: dict[str, Any]) -> None:
        """Reject scheduler-specific omissions before a script is rendered."""


class SlurmScheduler(Scheduler):
    type_name = "slurm"
    supports_bulk_status = True

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

    def validate_resources(self, resources: dict[str, Any]) -> None:
        """Reject unfilled placeholders before a script is rendered.

        sbatch happily accepts ``--account=<nersc_account>`` and either
        rejects it opaquely or -- worse, for mail_user -- succeeds silently
        with a broken value. Catch these here, symmetrically with the PBS
        adapter, so the failure names the field instead of the symptom.
        """
        placeholders = [
            label
            for label in ("account", "partition", "qos", "constraint",
                          "mail_user", "mail_type")
            if str(resources.get(label) or "").startswith("<")
        ]
        if placeholders:
            raise SchedulerError(
                "Slurm submission configuration has unfilled placeholders: "
                + ", ".join(placeholders)
                + ". Set them in machines.local.yaml (see the .example) or "
                "pass them in the submission request."
            )

    def bulk_status_argv(self, job_ids: Sequence[str]) -> list[str]:
        """Ask squeue about many jobs at once, tagging each answer with its id.

        Every remote call costs seconds of login-shell startup regardless of
        what it runs, so polling N jobs must not mean N round trips.
        """
        return [
            "squeue",
            "-j",
            ",".join(job_ids),
            "-h",
            "-o",
            "%i|%T",
        ]

    def parse_bulk_status(self, stdout: str) -> dict[str, str]:
        """Map job id -> normalized state from bulk squeue output.

        Jobs missing from the output have left the queue; the caller resolves
        those through accounting.
        """
        states: dict[str, str] = {}
        for line in stdout.strip().splitlines():
            if "|" not in line:
                continue
            raw_id, _, raw_state = line.partition("|")
            job_id = raw_id.strip()
            if not job_id:
                continue
            states[job_id] = _SLURM_STATES.get(
                raw_state.strip().split()[0].upper() if raw_state.strip() else "",
                UNKNOWN,
            )
        return states

    # Fields a watcher needs to describe a job without a second round trip:
    # id, name, state, elapsed, limit, nodes, reason, directory, start time.
    _USER_QUEUE_FORMAT = "%i|%j|%T|%M|%l|%D|%R|%Z|%S"

    def user_queue_argv(self, user: str) -> list[str]:
        return ["env", "TZ=UTC", "LC_ALL=C", "SLURM_TIME_FORMAT=standard",
                "squeue", "-u", user, "-h", "-o", self._USER_QUEUE_FORMAT]

    def parse_user_queue(self, stdout: str) -> list[dict[str, Any]]:
        fields = ("job_id", "name", "state", "elapsed", "time_limit",
                  "nodes", "reason", "work_dir")
        jobs: list[dict[str, Any]] = []
        for line in stdout.strip().splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < len(fields) or not parts[0]:
                continue
            job = dict(zip(fields, parts))
            raw_state = job["state"]
            job["raw_state"] = raw_state
            job["state"] = _SLURM_STATES.get(
                raw_state.split()[0].upper() if raw_state else "", UNKNOWN
            )
            # Slurm ids are already short; the field exists so that reporting
            # code can shorten a PBS id without knowing which scheduler it is.
            job["display_id"] = job["job_id"]
            job["estimated_start"] = (
                _start_time(parts[8])
                if job["state"] == PENDING and len(parts) > 8 else None
            )
            jobs.append(job)
        return jobs

    def accounting_argv(self, job_id: str) -> list[str]:
        """Query sacct for a finished job's state and exit code."""
        return ["sacct", "-j", job_id, "-n", "-P", "-o", "State,ExitCode"]

    def bulk_accounting_argv(self, job_ids: Sequence[str]) -> list[str]:
        """Ask sacct about many finished jobs at once."""
        return [
            "sacct",
            "-j",
            ",".join(job_ids),
            "-n",
            "-P",
            "-o",
            "JobID,State,ExitCode",
        ]

    def parse_bulk_accounting(self, stdout: str) -> dict[str, tuple[str, str | None]]:
        """Map job id -> (state, exit code) from bulk sacct output.

        sacct also lists ``.batch`` and ``.extern`` steps; those are skipped so
        the job's own row is what gets recorded.
        """
        results: dict[str, tuple[str, str | None]] = {}
        for line in stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            job_id = parts[0].strip()
            if not job_id or "." in job_id:
                continue
            raw_state = parts[1].strip()
            if not raw_state:
                continue
            state = _SLURM_STATES.get(raw_state.split()[0].upper(), UNKNOWN)
            exit_code = parts[2].strip() if len(parts) > 2 else None
            results.setdefault(job_id, (state, exit_code or None))
        return results

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
        job_id = text.splitlines()[-1].strip()
        if not re.fullmatch(
            r"\d+(?:\[\])?(?:[.][A-Za-z0-9_.-]+)?",
            job_id,
        ):
            raise SchedulerError(
                f"could not parse job id from qsub output: {stdout!r}"
            )
        return job_id

    def status_argv(self, job_id: str) -> list[str]:
        return ["qstat", "-x", "-f", job_id]

    def parse_status(self, stdout: str, returncode: int) -> str:
        match = re.search(r"job_state\s*=\s*(\w)", stdout)
        if not match:
            return UNKNOWN
        state = _PBS_STATES.get(match.group(1).upper(), UNKNOWN)
        if state not in TERMINAL_STATES:
            return state

        comment = re.search(r"^\s*comment\s*=\s*(.+)$", stdout, re.MULTILINE)
        if comment and re.search(r"\b(delet|cancel)", comment.group(1), re.IGNORECASE):
            return CANCELLED

        exit_code = self.parse_exit_code(stdout)
        if exit_code is None:
            # A terminal letter alone does not prove success. PBS extended
            # status normally carries Exit_status; stay conservative if it is
            # absent instead of silently classifying a failed job as complete.
            return UNKNOWN
        try:
            return COMPLETED if int(exit_code) == 0 else FAILED
        except ValueError:
            return UNKNOWN

    def parse_exit_code(self, stdout: str) -> str | None:
        """Extract PBS ``Exit_status`` from extended qstat output."""
        match = re.search(r"^\s*Exit_status\s*=\s*(\S+)", stdout, re.MULTILINE)
        return match.group(1) if match else None

    def accounting_argv(self, job_id: str) -> list[str]:
        """Ask about a job that has already left the queue.

        ``-x`` is what makes a finished job visible at all; without it qstat
        reports the id as unknown the moment the job ends. PBS keeps finished
        jobs only for a site-configured window, so a job that ended long ago
        legitimately resolves to ``unknown``.
        """
        return ["qstat", "-x", "-f", job_id]

    def parse_accounting(self, stdout: str) -> tuple[str, str | None]:
        """Reuse the extended-status parser, which already reads Exit_status."""
        return self.parse_status(stdout, 0), self.parse_exit_code(stdout)

    def user_queue_argv(self, user: str) -> list[str]:
        """Ask which job ids belong to ``user``, and nothing else.

        ``qstat -u`` exists but only in the table format, which truncates the
        job id at a fixed column width and marks the loss with ``*``. A
        truncated id cannot be used as a snapshot key or passed back to qstat,
        and widening the format with ``-w`` still truncates. ``qselect`` prints
        whole ids, one per line, and prints nothing for a user with no jobs --
        so the fragile column parsing disappears and ``user_queue_detail_argv``
        asks for the fields in a format meant to be machine-read.
        """
        return ["qselect", "-u", user]

    def parse_user_queue(self, stdout: str) -> list[dict[str, Any]]:
        """Turn the id list into skeleton jobs for the detail pass to fill in.

        States stay ``unknown`` here on purpose: qselect reports membership,
        not status. ``queue_watch`` treats a missing detail pass as an error
        rather than reporting these skeletons, so an unknown state never
        reaches a report.
        """
        jobs: list[dict[str, Any]] = []
        for line in stdout.strip().splitlines():
            job_id = line.strip()
            if not job_id or not job_id[0].isdigit():
                continue
            jobs.append(
                {
                    "job_id": job_id,
                    # PBS ids carry the whole server FQDN; the sequence number
                    # alone is what a human reads and what qstat accepts.
                    "display_id": job_id.split(".", 1)[0],
                    "name": "?",
                    "state": UNKNOWN,
                    "raw_state": "",
                    "nodes": "?",
                    "elapsed": "",
                    "time_limit": "",
                    "reason": "",
                    "work_dir": "?",
                }
            )
        return jobs

    def user_queue_detail_argv(self, job_ids: Sequence[str]) -> list[str] | None:
        """Describe the listed jobs in one JSON round trip.

        ``-F json`` is ignored when combined with ``-u``, which is the other
        reason the id list is fetched separately: given explicit ids, qstat
        emits structured output and no column truncation.
        """
        if not job_ids:
            return None
        return ["env", "TZ=UTC", "LC_ALL=C", "qstat", "-f", "-F", "json", *job_ids]

    def parse_user_queue_detail(self, stdout: str) -> dict[str, dict[str, Any]]:
        """Map full job id -> normalized fields from ``qstat -f -F json``."""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise SchedulerError(
                f"could not parse qstat JSON output: {error}"
            ) from error
        records = payload.get("Jobs")
        if not isinstance(records, dict):
            return {}

        detail: dict[str, dict[str, Any]] = {}
        for job_id, record in records.items():
            if not isinstance(record, dict):
                continue
            resources = record.get("Resource_List") or {}
            used = record.get("resources_used") or {}
            raw_state = str(record.get("job_state") or "").strip()
            estimated = record.get("estimated") or {}
            start = (estimated.get("start_time") if isinstance(estimated, dict)
                     else None)
            if start is None:
                start = record.get("estimated.start_time")
            detail[job_id] = {
                "name": str(record.get("Job_Name") or "?"),
                "raw_state": raw_state,
                "state": _PBS_STATES.get(raw_state.upper(), UNKNOWN),
                "nodes": str(resources.get("nodect") or "?"),
                "time_limit": str(resources.get("walltime") or ""),
                "elapsed": str(used.get("walltime") or ""),
                "queue": str(record.get("queue") or ""),
                "work_dir": self._work_dir(record),
                "reason": self._queue_reason(record),
                "estimated_start": (_start_time(start)
                                    if _PBS_STATES.get(raw_state.upper()) == PENDING
                                    else None),
            }
        return detail

    @staticmethod
    def _work_dir(record: dict[str, Any]) -> str:
        """Recover the submission directory from the job's output path.

        PBS does not report ``PBS_O_WORKDIR`` in qstat output on every site,
        but ``Output_Path`` is always set and defaults to the submission
        directory, which for this toolchain's jobs is the run directory. The
        value is ``host:/abs/path/name.oID``; the host prefix is dropped.
        """
        output_path = str(record.get("Output_Path") or "")
        _, _, remainder = output_path.partition(":")
        candidate = remainder or output_path
        if not candidate.startswith("/"):
            return str(record.get("jobdir") or "?")
        parent, _, _ = candidate.rpartition("/")
        return parent or "?"

    # PBS hold letters. Reported verbatim by qstat as Hold_Types; "n" means no
    # hold at all. A dependency hold is the normal state of a chained XGC
    # restart, so it has to read as intentional rather than as a problem.
    _HOLD_REASONS = {
        "u": "user hold",
        "o": "operator hold",
        "s": "system hold",
        "d": "dependency hold",
        "p": "bad password hold",
    }

    @classmethod
    def _queue_reason(cls, record: dict[str, Any]) -> str:
        """Explain why a job is not running, in one short phrase."""
        if str(record.get("job_state") or "").strip().upper() == "R":
            return ""

        bits: list[str] = []
        holds = str(record.get("Hold_Types") or "n").strip().lower()
        if holds and holds != "n":
            bits.extend(
                cls._HOLD_REASONS.get(letter, f"hold {letter}") for letter in holds
            )
        dependency = str(record.get("depend") or "").strip()
        if dependency:
            bits.append(cls._short_dependency(dependency))
        if not bits:
            # qstat's comment is the scheduler's own explanation, e.g.
            # "Not Running: Insufficient amount of resource". It can also be a
            # multi-line node list once the job starts, hence the truncation.
            comment = str(record.get("comment") or "").strip()
            if comment:
                bits.append(comment.splitlines()[0][:120])
        return "; ".join(bits)

    @staticmethod
    def _short_dependency(dependency: str) -> str:
        """Shorten ``afterany:123.server@server`` to ``afterany:123``."""
        kind, _, remainder = dependency.partition(":")
        ids = [
            entry.strip().split("@", 1)[0].split(".", 1)[0]
            for entry in remainder.split(",")
            if entry.strip()
        ]
        return f"{kind}:{','.join(ids)}" if ids else kind

    def cancel_argv(self, job_id: str) -> list[str]:
        return ["qdel", job_id]

    def validate_resources(self, resources: dict[str, Any]) -> None:
        """Require the site resources used by the Polaris PBS template."""
        missing = [
            label
            for label in ("account", "queue", "filesystems", "system")
            if not resources.get(label)
            or str(resources[label]).startswith("<")
        ]
        if missing:
            raise SchedulerError(
                "PBS submission configuration is incomplete; set "
                + ", ".join(missing)
                + " in machines.yaml or the submission request"
            )


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
