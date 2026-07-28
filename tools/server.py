"""MCP server for XGC automation.

opencode runs this module via mcp.xgc-tools in `opencode.json`:
    uv run python -m tools.server

Tools implemented so far:
  - list_machines : machines defined in machines.yaml
  - preflight     : check connectivity/auth/config state (roadmap item 1)
  - plan_job      : render a job script and estimate cost, submitting nothing
  - submit_job    : submit to the scheduler, gated on explicit confirmation
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

from tools import hpc_runner as hr
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
        account=account,
        partition=partition,
        hello_world=hello_world,
    )
    return plan.to_dict()


@mcp.tool()
def submit_job(
    machine: str,
    confirm: bool = False,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    hello_world: bool = False,
) -> dict[str, Any]:
    """Submit a job to the scheduler. Requires explicit human confirmation.

    With confirm=False (the default) this submits nothing and returns the plan.
    Only set confirm=True after the user has seen the plan and approved it in
    this conversation. Never set it on your own initiative.

    On success, writes a provenance record to runs/<run_id>/.
    """
    return hr.submit(
        machine,
        confirm=confirm,
        job_name=job_name,
        nodes=nodes,
        walltime=walltime,
        ntasks_per_node=ntasks_per_node,
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
