"""Capture XGC's build banner from a job's stdout into provenance.

Every XGC binary prints a self-description at startup -- build date, compiler,
build type, and a GIT INFO block naming the exact commit and whether the
working tree was dirty (CMake/write_buildinfo generates it at build time).
That banner is the ground truth about what actually ran: the provenance
recorded at submission says which binary path was *planned*, but the binary
may have been rebuilt between plan and execution, and externally submitted
runs have no plan at all.

This module reads a bounded slice of the job's stdout on the remote machine
(never the whole file -- XGC logs grow large), parses the banner, and hands a
structured record back to the callers in ``hpc_runner`` and ``refresh`` that
store it in the run's provenance and catalog entry.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from tools import machines as m
from tools import ssh

# The banner is printed once, early in stdout, but its byte offset scales with
# job size (the MPI node map above it is one line per node). Scanning is
# line-bounded instead of byte-bounded so a 1000-node job's map cannot push
# the banner out of the window.
_SCAN_LINE_LIMIT = 200_000
# Lines kept once "BUILD DATE:" is seen: the banner itself is ~12 lines, and a
# dirty build appends one line per changed tracked file.
_BANNER_LINE_LIMIT = 200
# Changed-file lists beyond this are summarized, not stored.
_CHANGED_FILES_LIMIT = 100

_REMOTE_RE = re.compile(r"^\s+(\S+)\s+(\S+)\s+\(fetch\)$")
_COMMIT_RE = re.compile(r"^\s+([0-9a-f]{40})\s+(.*)$")
_HEADER_FIELDS = {
    "BUILD DATE:": "build_date",
    "HOST:": "build_host",
    "BUILT BY:": "built_by",
    "CXX COMPILER:": "cxx_compiler",
    "BUILD TYPE:": "build_type",
}


def parse_build_banner(text: str) -> dict[str, Any] | None:
    """Parse the startup banner out of (a slice of) XGC stdout.

    Returns None when no ``BUILD DATE:`` header is present, which is the
    normal case for non-XGC jobs such as scheduler hello-worlds.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("BUILD DATE:")), None
    )
    if start is None:
        return None

    banner: dict[str, Any] = {key: None for key in _HEADER_FIELDS.values()}
    banner["xgc_flags"] = []
    git: dict[str, Any] = {
        "remotes": {},
        "commit": None,
        "commit_subject": None,
        "working_tree": "unknown",
    }

    section = "header"
    for line in lines[start:]:
        stripped = line.strip()
        if section == "header":
            for label, field in _HEADER_FIELDS.items():
                if line.startswith(label):
                    banner[field] = line[len(label):].strip() or None
                    break
            else:
                if stripped == "XGC_FLAGS:":
                    section = "flags"
                elif stripped == "GIT INFO:":
                    section = "git"
            continue
        if section == "flags":
            if stripped == "GIT INFO:":
                section = "git"
            elif stripped:
                banner["xgc_flags"].append(stripped)
            continue
        # section == "git": consume until a line that is not part of the block.
        if not stripped:
            continue
        remote = _REMOTE_RE.match(line)
        if remote:
            git["remotes"][remote.group(1)] = remote.group(2)
            continue
        commit = _COMMIT_RE.match(line)
        if commit:
            git["commit"] = commit.group(1)
            git["commit_subject"] = commit.group(2).strip() or None
            continue
        if stripped == "Working tree is clean.":
            git["working_tree"] = "clean"
            break
        if stripped == "Uncommitted changes exist.":
            git["working_tree"] = "dirty"
            git["changed_files"] = []
            continue
        if stripped == "Whole-repo changed tracked files vs HEAD:":
            continue
        if stripped.startswith("XGC_core diff preview file:"):
            git["diff_preview_file"] = stripped.split(":", 1)[1].strip()
            break
        if stripped == "Build not attached to a git repository.":
            git["working_tree"] = "no-git"
            break
        if git.get("working_tree") == "dirty":
            files = git["changed_files"]
            if len(files) < _CHANGED_FILES_LIMIT:
                files.append(stripped)
            else:
                git["changed_files_truncated"] = True
            continue
        break  # first line that belongs to neither the banner nor GIT INFO

    banner["git"] = git
    return banner


def select_stdout_file(names: list[str], job_id: str | None) -> str | None:
    """Pick the stdout file for one job out of a shared run directory.

    Restart attempts routinely share a case directory, so several scheduler
    stdout files coexist and "the .out file" is ambiguous. The job id
    disambiguates: Slurm writes ``slurm-<jobid>.out``, PBS ``<name>.o<jobid>``.
    Without a job-id match, a lone .out file is accepted; several are not.
    """
    outs = [n for n in names if n.endswith(".out") or re.search(r"\.o\d+$", n)]
    if job_id:
        job = str(job_id)
        matched = [n for n in outs if job in n]
        if len(matched) == 1:
            return matched[0]
        if matched:
            return sorted(matched)[-1]
    if len(outs) == 1:
        return outs[0]
    return None


def capture(machine: str, remote_run_dir: str, job_id: str | None) -> dict[str, Any]:
    """Read and parse the build banner from a run's stdout on the machine.

    Bounded and read-only. The result always says what happened:

    * ``found: True`` with ``banner`` -- parsed successfully.
    * ``found: False`` with ``definitive: True`` -- stdout was read but holds
      no banner (a non-XGC job); callers may record that and stop retrying.
    * ``found: False`` with ``definitive: False`` -- transient (no stdout file
      yet, ambiguous directory, ssh failure); callers should retry later.
    """
    base = {"found": False, "definitive": False}
    try:
        cfg = m.get_machine(machine)
        host = ssh.host_for(cfg)
    except (KeyError, ssh.SSHError) as exc:
        return {**base, "reason": str(exc)}

    run_dir = remote_run_dir.rstrip("/")
    listing = ssh.run(host, ["ls", "-1", run_dir])
    if not listing.ok:
        return {**base, "reason": f"cannot list {run_dir}"}
    name = select_stdout_file(listing.stdout.split(), job_id)
    if name is None:
        return {**base, "reason": f"no unambiguous stdout file in {run_dir}"}

    program = (
        f"NR>{_SCAN_LINE_LIMIT}{{exit}} "
        "/^BUILD DATE:/{found=1} "
        f"found{{print; n++}} found&&n>={_BANNER_LINE_LIMIT}{{exit}}"
    )
    result = ssh.run(host, ["awk", program, f"{run_dir}/{name}"])
    if not result.ok:
        return {**base, "reason": f"cannot read {run_dir}/{name}", "source_file": name}

    banner = parse_build_banner(result.stdout)
    if banner is None:
        # The file exists and was read; an XGC banner prints within the first
        # seconds of execution, so its absence is meaningful once the job has
        # actually started. The caller decides finality from the job state.
        return {
            **base,
            "definitive": True,
            "reason": f"no build banner in {name}",
            "source_file": name,
        }
    return {
        "found": True,
        "definitive": True,
        "banner": banner,
        "source_file": name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def catalog_summary(capture_result: dict[str, Any]) -> dict[str, Any]:
    """Compact subset of a successful capture for the catalog's run entry.

    The full banner (flags, every remote, changed-file list) lives in the
    run's provenance.json; the catalog keeps what search and `xgc-sim show`
    need.
    """
    banner = capture_result["banner"]
    git = banner.get("git") or {}
    remotes = git.get("remotes") or {}
    return {
        "build_date": banner.get("build_date"),
        "build_type": banner.get("build_type"),
        "cxx_compiler": banner.get("cxx_compiler"),
        "built_by": banner.get("built_by"),
        "commit": git.get("commit"),
        "commit_subject": git.get("commit_subject"),
        "working_tree": git.get("working_tree"),
        "origin_url": remotes.get("origin") or next(iter(remotes.values()), None),
        "source_file": capture_result.get("source_file"),
        "captured_at": capture_result.get("captured_at"),
    }
