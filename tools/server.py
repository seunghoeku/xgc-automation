"""MCP server for XGC automation.

opencode runs this module via mcp.xgc-tools in `opencode.json`:
    uv run python -m tools.server

Tools implemented so far:
  - list_machines : machines defined in machines.yaml
  - preflight     : check connectivity/auth/config state (roadmap item 1)
  - plan_job      : render a job script and estimate cost, submitting nothing
  - list_cases    : baseline XGC cases known to validate
  - describe_parameter : an input parameter's type/default/options, from XGC source
  - plan_input    : apply overrides to a baseline and return a diff
  - stage_case    : copy a baseline's files to a new working directory
  - write_input   : write a modified input, gated on confirmation
  - validate_input: check an XGC input via `xgc --dryrun` on the login node
  - submit_job    : submit to the scheduler, gated on confirmation + dryrun
  - job_status    : normalized job state across slurm/pbs
  - cancel_job    : cancel a job, also gated on confirmation
  - job_output    : bounded tail of a run's stdout/stderr

The remaining tools (physics_advisor / input_builder / validator /
data_analyzer) will be added following the roadmap.
"""

from __future__ import annotations

import os
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

from tools import dryrun
from tools import hpc_runner as hr
from tools import input_builder as ib
from tools import machines as m

mcp = FastMCP("xgc-tools")


@mcp.tool()
def list_machines() -> dict[str, Any]:
    """Return the list of HPC machines defined in machines.yaml with a summary."""
    out: dict[str, Any] = {}
    for name in m.machine_names():
        cfg = m.get_machine(name)
        out[name] = {
            "center": cfg.get("center"),
            "host": cfg.get("host"),
            "scheduler": cfg.get("scheduler", {}).get("type"),
            "auth": cfg.get("auth", {}).get("method"),
        }
    return {"machines": out}


def _check_placeholders(cfg: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively collect paths of values still left as `<...>` placeholders."""
    unfilled: list[str] = []
    for key, value in cfg.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            unfilled.extend(_check_placeholders(value, path))
        elif m.is_placeholder(value):
            unfilled.append(path)
    return unfilled


def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _control_socket_alive(ssh_host: str, timeout: float = 5.0) -> dict[str, Any]:
    """Check whether the ssh ControlMaster socket is alive (`ssh -O check`).

    This is the key indicator for MFA-protected machines. If the socket is alive,
    commands can be sent without re-authenticating; if not, the user must open a
    session themselves. We never authenticate on their behalf (AGENTS.md,
    absolute rule 3).
    """
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check", ssh_host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"alive": False, "detail": "ssh executable not found"}
    except subprocess.TimeoutExpired:
        return {"alive": False, "detail": "ssh -O check timed out"}

    # When alive, ssh prints "Master running (pid=...)" to stderr and returns 0
    msg = (proc.stderr or proc.stdout or "").strip()
    alive = proc.returncode == 0
    # Distinguish "no ControlPath configured at all" from "master is dead"
    configured = "no controlpath" not in msg.lower()
    return {
        "alive": alive,
        "multiplexing_configured": configured,
        "detail": msg or ("master running" if alive else "no master"),
    }


def _ohmic_reachable(timeout: float = 4.0) -> dict[str, Any]:
    """Check reachability of the ohmic API endpoint only (no auth, no inference)."""
    base = os.environ.get("OHMIC_BASE_URL", "https://api.ohmic.pppl.gov/v1")
    key_present = bool(os.environ.get("OHMIC_API_KEY"))
    try:
        req = urllib.request.Request(base, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        reachable = True
        detail = "HEAD returned 2xx"
    except urllib.error.HTTPError as exc:
        # Even a 4xx/5xx means TCP+TLS reached the endpoint
        reachable = True
        detail = f"HTTP {exc.code} (reached)"
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reachable = False
        detail = f"unreachable: {exc}"
    return {"endpoint": base, "reachable": reachable, "api_key_present": key_present, "detail": detail}


@mcp.tool()
def preflight(machine: str | None = None) -> dict[str, Any]:
    """Check connectivity, authentication, and configuration state before submitting.

    Args:
        machine: check only this machine. If omitted, check all machines plus ohmic.
    Returns:
        Per-item status (ok/warn/fail) with human-readable diagnostics.
    """
    report: dict[str, Any] = {"ohmic": _ohmic_reachable()}

    targets = [machine] if machine else m.machine_names()
    machines_report: dict[str, Any] = {}
    for name in targets:
        try:
            cfg = m.get_machine(name)
        except KeyError as exc:
            machines_report[name] = {"status": "fail", "error": str(exc)}
            continue

        unfilled = _check_placeholders(cfg)
        auth = cfg.get("auth", {})
        host = cfg.get("host")

        # If host is not a placeholder, do a light SSH port (22) reachability check
        ssh_ok = None
        if host and not m.is_placeholder(host):
            ssh_ok = _tcp_reachable(host, 22)

        # MFA machines: a live ControlMaster socket is required to send commands
        # without re-authenticating.
        control = None
        ssh_alias = auth.get("ssh_config_host")
        if ssh_alias and not m.is_placeholder(ssh_alias):
            control = _control_socket_alive(ssh_alias)

        # Only fields that decide whether submission is possible count as blockers
        blockers = [f for f in unfilled if f.startswith("scheduler.")]
        actions: list[str] = []
        if ssh_ok is False:
            actions.append(f"{host}:22 unreachable — check VPN/on-site network")
        if control is not None and not control["alive"]:
            if not control.get("multiplexing_configured", True):
                actions.append(
                    f"no ControlMaster settings for Host {ssh_alias} in ~/.ssh/config — "
                    f"add them per docs/ssh-setup.md (prerequisite for avoiding MFA re-auth)"
                )
            else:
                actions.append(
                    f"ssh ControlMaster not running — run `ssh {ssh_alias}` yourself to "
                    f"authenticate via MFA; the session is then reused"
                )
        if blockers:
            actions.append(
                f"scheduler settings unconfirmed: {', '.join(blockers)} — "
                f"do not submit until resolved"
            )

        if blockers or ssh_ok is False:
            status = "fail"
        elif unfilled or (control is not None and not control["alive"]):
            status = "warn"
        else:
            status = "ok"

        machines_report[name] = {
            "status": status,
            "host": host,
            "scheduler": cfg.get("scheduler", {}).get("type"),
            "auth_method": auth.get("method"),
            "mfa": bool(auth.get("mfa")),
            "ssh_port_22_reachable": ssh_ok,
            "control_master": control,
            "unfilled_config_fields": unfilled,
            "submit_blockers": blockers,
            "next_actions": actions,
        }

    report["machines"] = machines_report
    return report


@mcp.tool()
def plan_job(
    machine: str,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    cpus_per_task: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    hello_world: bool = False,
) -> dict[str, Any]:
    """Render a job script and estimate its cost WITHOUT submitting anything.

    Always call this before submit_job, and show the user the rendered script
    plus the node-hour estimate (AGENTS.md, absolute rules 1 and 4).

    Args:
        machine: machine key from machines.yaml (see list_machines).
        hello_world: run `srun hostname` instead of XGC, for round-trip testing.
    Unspecified resources fall back to the machine's defaults.
    """
    plan = hr.prepare_submission(
        machine,
        job_name=job_name,
        nodes=nodes,
        walltime=walltime,
        ntasks_per_node=ntasks_per_node,
        cpus_per_task=cpus_per_task,
        account=account,
        partition=partition,
        hello_world=hello_world,
    )
    return plan.to_dict()


@mcp.tool()
def list_cases() -> dict[str, Any]:
    """List known baseline XGC cases from kb/cases/.

    These are inputs verified to pass `xgc --dryrun`. Start from one of these
    rather than writing a namelist from scratch.
    """
    return {"cases": ib.list_cases()}


@mcp.tool()
def describe_parameter(machine: str, name: str) -> dict[str, Any]:
    """Look up an XGC input parameter's type, default, description, and options.

    Answers come from metadata generated from the XGC C++ source on `machine`,
    so they reflect the version actually installed there. Use this instead of
    recalling parameter semantics from memory, and to check a name before
    proposing a change. Unknown names come back with `did_you_mean`.

    Watch the units: several inputs are given in normalized form and converted
    internally, so the description is authoritative.
    """
    return ib.describe_parameter(machine, name)


@mcp.tool()
def plan_input(
    case: str,
    overrides: dict[str, Any],
    target_path: str,
    machine: str | None = None,
) -> dict[str, Any]:
    """Apply parameter overrides to a baseline case and return a diff. Writes nothing.

    Every override is validated against XGC's metadata first: unknown names,
    invalid enum values, and type mismatches are rejected here.

    Show the user the returned `diff` and `changes` before calling write_input.

    Args:
        overrides: {parameter_name: value}. Booleans and strings are rendered
            in Fortran form automatically.
        target_path: where the new input will go. Must not be the baseline.
    """
    plan = ib.plan_input(case, overrides, target_path=target_path, machine=machine)
    return plan.to_dict()


@mcp.tool()
def stage_case(
    case: str,
    target_dir: str,
    confirm: bool = False,
    machine: str | None = None,
) -> dict[str, Any]:
    """Copy a baseline case's files (mesh, equilibrium, profiles) to a new directory.

    Do this before writing a modified input, since XGC needs those files
    alongside it. The copy happens remotely; nothing transits this machine.
    Requires explicit confirmation.
    """
    return ib.stage_case(case, target_dir, confirm=confirm, machine=machine)


@mcp.tool()
def write_input(
    case: str,
    overrides: dict[str, Any],
    target_path: str,
    confirm: bool = False,
    machine: str | None = None,
    n_ranks: int | None = None,
) -> dict[str, Any]:
    """Write a modified input file to the remote. Requires explicit confirmation.

    With confirm=False this returns the plan and writes nothing. Only set
    confirm=True after the user has seen the diff and approved it.

    Refuses to overwrite a baseline case directory. After writing, validates
    with `xgc --dryrun` and reports the result.
    """
    plan = ib.plan_input(case, overrides, target_path=target_path, machine=machine)
    return ib.write_input(plan, confirm=confirm, n_ranks=n_ranks)


@mcp.tool()
def validate_input(
    machine: str,
    case_dir: str,
    n_ranks: int | None = None,
) -> dict[str, Any]:
    """Validate an XGC input file by running `xgc --dryrun` on the login node.

    Runs the real XGC binary, so it catches exactly what a real run would
    reject: removed/deprecated parameters, mesh problems, memory overruns, and
    low particle resolution. Takes seconds, uses one rank, and consumes no
    allocation.

    Run this before submitting any XGC job. Report `removed` entries to the user
    as blocking errors -- the job cannot run until they are fixed.

    Args:
        case_dir: remote directory containing `input` and `input_dir/`.
        n_ranks: rank count the real job will use, so estimates match.
    """
    return dryrun.run_dryrun(machine, case_dir, n_ranks=n_ranks).to_dict()


@mcp.tool()
def submit_job(
    machine: str,
    confirm: bool = False,
    case_dir: str | None = None,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    cpus_per_task: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    hello_world: bool = False,
    skip_dryrun: bool = False,
) -> dict[str, Any]:
    """Submit a job to the scheduler. Requires explicit human confirmation.

    With confirm=False (the default) this submits nothing and returns the plan.
    Only set confirm=True after the user has seen the plan and approved it in
    this conversation. Never set it on your own initiative.

    When case_dir is given, `xgc --dryrun` validates the input first and a
    rejected input blocks submission. Always pass case_dir for real XGC runs.
    Do not set skip_dryrun unless the user explicitly asks for it.

    On success, writes a provenance record to runs/<run_id>/.
    """
    return hr.submit(
        machine,
        confirm=confirm,
        case_dir=case_dir,
        skip_dryrun=skip_dryrun,
        job_name=job_name,
        nodes=nodes,
        walltime=walltime,
        ntasks_per_node=ntasks_per_node,
        cpus_per_task=cpus_per_task,
        account=account,
        partition=partition,
        hello_world=hello_world,
    )


@mcp.tool()
def job_status(machine: str, job_id: str) -> dict[str, Any]:
    """Query a job's state, normalized to pending/running/completed/failed/cancelled.

    For Slurm, finished jobs leave the queue, so this falls back to sacct to
    recover the final state and exit code.
    """
    return hr.status(machine, job_id)


@mcp.tool()
def cancel_job(machine: str, job_id: str, confirm: bool = False) -> dict[str, Any]:
    """Cancel a running or queued job. Requires explicit human confirmation."""
    return hr.cancel(machine, job_id, confirm=confirm)


@mcp.tool()
def job_output(machine: str, run_id: str, max_bytes: int = 100_000) -> dict[str, Any]:
    """Read the tail of a run's stdout/stderr from its remote run directory.

    Reads a bounded tail instead of downloading files: XGC output can reach
    terabytes and must be reduced remotely (AGENTS.md, data principles).
    """
    return hr.fetch_output(machine, run_id, max_bytes=max_bytes)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
