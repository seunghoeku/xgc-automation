"""MCP server for XGC automation.

opencode runs this module via mcp.xgc-tools in `opencode.json`:
    uv run python -m tools.server

Tools implemented so far:
  - list_machines : machines defined in machines.yaml
  - preflight     : check connectivity/auth/config state (roadmap item 1)
  - plan_simulation_id : preview the next global simulation ID and directory
  - reserve_simulation_id : reserve that ID after human confirmation
  - stage_simulation_case : stage a baseline under the canonical directory
  - search_simulations : search the catalog of past and current simulations
  - show_simulation : one simulation's full record, runs, and provenance
  - simulation_stats : catalog counts by status/machine/origin
  - refresh_simulations : poll schedulers for every in-flight run
  - set_simulation_note : write the human-owned note, gated on confirmation
  - adopt_remote_run : catalogue a hand-submitted run from its remote directory, gated on confirm
  - inspect_input_archive : rank recent read-only input backups for migration
  - rank_promotion_candidates : join the archive with the simulation notebook
  - plan_case_promotion : draft a kb case from an archived input, writing nothing
  - write_draft_case : write that draft under kb/cases/drafts/, gated on confirm
  - physics_advisor : match natural-language physics to validated cases/rules
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
  - analyze_run   : remotely reduce a run to a bounded local summary
  - plan_remote_gui : resolve and validate an XGC_GUI Monitor target
  - launch_remote_gui : start a remote Monitor and local SSH tunnel
  - remote_gui_status / stop_remote_gui : manage launcher-owned GUI sessions

Scientific data products and provenance hardening will be added following the
roadmap.
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

from tools import case_promotion as cp
from tools import data_analyzer as da
from tools import dryrun
from tools import hpc_runner as hr
from tools import input_builder as ib
from tools import input_archive
from tools import machines as m
from tools import physics_advisor as pa
from tools import refresh as refresh_tool
from tools import remote_gui as rg
from tools import simulation_catalog as sc
from tools import simulation_registry as sr
from tools import ssh as ssh_transport

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
    alive, msg = ssh_transport.control_socket_alive(ssh_host, timeout=timeout)
    # Distinguish "no ControlPath configured at all" from "master is dead"
    configured = "no controlpath" not in msg.lower()
    result = {
        "alive": alive,
        "multiplexing_configured": configured,
        "detail": msg or ("master running" if alive else "no master"),
    }
    if alive:
        usable, channel_detail = ssh_transport.command_channel_usable(
            ssh_host, timeout=max(timeout, ssh_transport.COMMAND_CHANNEL_TIMEOUT_SECONDS)
        )
        result["command_channel_usable"] = usable
        result["command_channel_detail"] = channel_detail
    return result


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
        if auth.get("method") == ssh_transport.LOCAL_METHOD:
            # A local checkout never uses the ssh alias or the remote host name.
            unfilled = [f for f in unfilled if f not in ("auth.ssh_config_host", "host", "user")]
        host = cfg.get("host")

        # If host is not a placeholder, do a light SSH port (22) reachability check.
        # A local checkout (auth.method: local) runs on the machine itself: no
        # port, socket or certificate to check.
        is_local = auth.get("method") == ssh_transport.LOCAL_METHOD
        ssh_ok = None
        if host and not m.is_placeholder(host) and not is_local:
            ssh_ok = _tcp_reachable(host, 22)

        # MFA machines require a live ControlMaster. NERSC sshproxy machines
        # instead use their current key/certificate directly and explicitly
        # bypass multiplexing.
        control = None
        certificate = None
        direct_key = None
        ssh_alias = auth.get("ssh_config_host")
        if is_local:
            pass
        elif ssh_alias and not m.is_placeholder(ssh_alias):
            if auth.get("method") == "sshproxy":
                certificate = ssh_transport.certificate_status(ssh_alias)
                if certificate is None:
                    certificate = {
                        "present": False,
                        "valid": False,
                        "detail": "no CertificateFile is configured for the SSH alias",
                    }
                if certificate.get("valid", False):
                    usable, detail = ssh_transport.direct_key_channel_usable(ssh_alias)
                    direct_key = {"usable": usable, "detail": detail}
            else:
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
        elif control is not None and not control.get("command_channel_usable", True):
            actions.append(
                f"ssh master exists but cannot open a command channel — close any "
                f"interactive shell using the only channel, or run `ssh -O exit "
                f"{ssh_alias}` and reconnect; do not submit until a BatchMode `true` probe succeeds"
            )
        if blockers:
            actions.append(
                f"scheduler settings unconfirmed: {', '.join(blockers)} — "
                f"do not submit until resolved"
            )
        if certificate is not None and not certificate.get("valid", False):
            actions.append(
                f"SSH certificate is missing or expired ({certificate.get('detail')}) — "
                "refresh it with the user's NERSC sshproxy workflow"
            )
        if direct_key is not None and not direct_key["usable"]:
            actions.append(
                f"direct SSH key connection failed ({direct_key['detail']}) — "
                "verify IdentityFile/CertificateFile in the SSH alias and refresh sshproxy"
            )

        if (
            blockers
            or ssh_ok is False
            or (certificate is not None and not certificate.get("valid", False))
            or (direct_key is not None and not direct_key["usable"])
            or (control is not None and not control.get("command_channel_usable", True))
        ):
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
            "local_checkout": is_local,
            "mfa": bool(auth.get("mfa")),
            "ssh_port_22_reachable": ssh_ok,
            "control_master": control,
            "direct_key_connection": direct_key,
            "ssh_certificate": certificate,
            "unfilled_config_fields": unfilled,
            "submit_blockers": blockers,
            "next_actions": actions,
        }

    report["machines"] = machines_report
    return report


@mcp.tool()
def plan_job(
    machine: str,
    simulation_id: str | None = None,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    cpus_per_task: int | None = None,
    gpus_per_node: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    hello_world: bool = False,
    daos: bool = False,
    spare_nodes: int = 0,
    depend_on: str | None = None,
) -> dict[str, Any]:
    """Render a job script and estimate its cost WITHOUT submitting anything.

    Always call this before submit_job, and show the user the rendered script
    plus the node-hour estimate (AGENTS.md, absolute rules 1 and 4).

    Args:
        machine: machine key from machines.yaml (see list_machines).
        partition: Slurm partition or PBS queue.
        hello_world: run hostname through the machine launcher instead of XGC.
        daos: execute inside the machine's DAOS container (Aurora) instead of
            the parallel filesystem; logs and input stay in the case directory.
        spare_nodes: request extra nodes and run only on the ones whose GPUs
            pass a probe (Polaris; kb/bad_nodes.yaml hosts are skipped, and
            early crashes keep dropping nodes and relaunching while ``nodes``
            healthy ones remain). ``nodes`` stays the compute node count;
            the allocation and the cost estimate grow by ``spare_nodes``.
        depend_on: scheduler job id to wait for (afterany). Use it to chain a
            restart behind the job currently running in the same directory.
    Unspecified resources fall back to the machine's defaults.
    """
    plan = hr.prepare_submission(
        machine,
        simulation_id=simulation_id,
        job_name=job_name,
        nodes=nodes,
        walltime=walltime,
        ntasks_per_node=ntasks_per_node,
        cpus_per_task=cpus_per_task,
        gpus_per_node=gpus_per_node,
        account=account,
        partition=partition,
        hello_world=hello_world,
        daos=daos,
        spare_nodes=spare_nodes,
        depend_on=depend_on,
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
def plan_simulation_id(
    machines: list[str],
    short_name: str = "",
    tags: list[str] | None = None,
    kind: str = "production",
) -> dict[str, Any]:
    """Preview the next simulation ID and canonical directory name.

    ``kind`` picks the series. ``production`` (the default) is the physics
    sequence, ``n671pe``; use it for any run whose results will be studied.
    ``dev`` is a separate sequence, ``d0001pe``, for short test, debug, and
    algorithm-verification runs made while developing the code, so they stop
    consuming physics numbers. When the purpose is unclear, ask the human.

    Numbers are sequential across all machines within a series and never
    reused. Machine names are recorded as metadata but removed from the
    directory suffix because the machine codes are already present in the ID.
    This writes nothing.
    """
    return sr.plan_simulation_id(machines, short_name, tags=tags, kind=kind)


@mcp.tool()
def reserve_simulation_id(
    machines: list[str],
    short_name: str = "",
    tags: list[str] | None = None,
    expected_number: int | None = None,
    confirm: bool = False,
    kind: str = "production",
) -> dict[str, Any]:
    """Reserve the next simulation number. Requires explicit confirmation.

    First call plan_simulation_id and show its ID/directory to the human. After
    approval, pass that exact number as expected_number with confirm=True and
    the same ``kind`` (``production`` or ``dev``) as the plan. A stale number
    is rejected rather than silently allocating a different ID.
    """
    return sr.reserve_simulation_id(
        machines,
        short_name,
        tags=tags,
        expected_number=expected_number,
        confirm=confirm,
        kind=kind,
    )


@mcp.tool()
def stage_simulation_case(
    case: str,
    simulation_id: str,
    machine: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Copy a baseline into the reserved simulation's canonical directory.

    The path is derived from machines.yaml and the registry, not supplied by
    the model. Requires explicit confirmation before creating/copying files.
    """
    return sr.stage_simulation_case(
        case,
        simulation_id,
        machine=machine,
        confirm=confirm,
    )


@mcp.tool()
def search_simulations(
    query: str = "",
    machine: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    simulation_tag: str | None = None,
    sub_tag: str | None = None,
    origin: str | None = None,
    kind: str | None = None,
    case: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search the simulation catalog: every past and current simulation.

    ``kind`` separates ``production`` physics runs (``n``-numbered) from
    ``dev`` test and debug runs (``d``-numbered); leave it unset for both.

    ``query`` matches any text field, including the human note and the original
    notebook description. Check ``origin`` before relying on a match, because
    it bounds what the record can claim:

    - ``automation``: run through this toolchain; has linked runs, an input
      hash, and a dryrun report.
    - ``adopted``: real runs recorded before the catalog existed; job IDs and
      provenance are trustworthy, but the outcome was never captured, so the
      status may say ``submitted`` for work that finished long ago.
    - ``notebook``: the researcher's historical spreadsheet. Evidence of intent
      and outcome in their own words, with no machine-verified provenance.

    Use this before proposing a new simulation, to find what was already tried.
    The same search is available offline as ``xgc-sim search``.
    """
    matches = sc.search(
        query,
        machine=machine,
        status=status,
        tag=tag,
        simulation_tag=simulation_tag,
        sub_tag=sub_tag,
        origin=origin,
        kind=kind,
        case=case,
        after=after,
        before=before,
        limit=limit,
        catalog_dir=None,
    )
    return {"query": query, "count": len(matches), "matches": matches}


@mcp.tool()
def show_simulation(record_id: str) -> dict[str, Any]:
    """Return one simulation's full catalog record.

    Includes its status, linked scheduler runs with their provenance paths, the
    input hash, the archive location, and any historical notebook evidence.
    """
    return sc.show(record_id)


@mcp.tool()
def simulation_stats() -> dict[str, Any]:
    """Counts of catalog simulations by status, machine, and origin."""
    return sc.stats()


@mcp.tool()
def refresh_simulations(
    machine: str | None = None,
    record_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ask the schedulers about every in-flight run and record the answers.

    Use this before reporting on the state of ongoing work, since a simulation
    that was submitted and not polled since still reads as ``submitted``.

    This only writes down what the scheduler already decided, so it needs no
    confirmation. It polls and never submits or cancels. On machines with
    ``campaign.auto: true`` a simulation that turns completed gets its
    campaign archive started on the login node (reported under
    ``campaigns``); nothing existing is overwritten.
    Pass dry_run=True to see what would be polled without contacting anything.

    A run that hit the wall clock reports ``timeout`` and leaves the simulation
    ``interrupted``, meaning "restart to continue" rather than "broken".
    """
    if dry_run:
        return refresh_tool.plan_refresh(machine=machine, record_id=record_id)
    return refresh_tool.refresh(machine=machine, record_id=record_id)


@mcp.tool()
def build_campaign(
    record_id: str,
    confirm: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """Package a completed simulation as an HPC-Campaign ``.aca`` archive.

    Runs XGC-Analysis's packager detached on the machine's login node and
    writes ``<run_dir>/<record_id>.aca``; the result is collected later by
    ``campaign_status`` or ``refresh_simulations``. Machines with
    ``campaign.auto: true`` do this by themselves when a production
    simulation completes; this tool is for other runs, retries, and
    rebuilds. With confirm=False it only returns the plan and the command.
    replace=True overwrites an existing archive: pass it only after the user
    agreed to that.
    """
    from tools import campaign

    try:
        return campaign.build(record_id, confirm=confirm, replace=replace)
    except campaign.CampaignError as exc:
        return {"started": False, "error": str(exc)}


@mcp.tool()
def campaign_status(record_id: str) -> dict[str, Any]:
    """Report a simulation's campaign archive state; collects a finished build."""
    from tools import campaign

    try:
        return campaign.status(record_id)
    except campaign.CampaignError as exc:
        return {"error": str(exc)}


@mcp.tool()
def adopt_remote_run(
    machine: str,
    remote_dir: str,
    confirm: bool = False,
    tags: list[str] | None = None,
    case: str | None = None,
    note: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Adopt a run that was submitted by hand, from its remote directory.

    Reads the directory's input, job scripts, stdout banners and the
    scheduler's history of its jobs; writes an ``origin: adopted`` catalog
    record, appends the input to input_backup/, and keeps the script and
    banners under runs/adopted/. Never assigns a sequence number. With
    confirm=False it only returns the record it would write. Only set
    confirm=True after the user has approved that record.
    """
    from tools import remote_adoption

    try:
        return remote_adoption.adopt_remote(
            machine,
            remote_dir,
            confirm=confirm,
            tags=tags,
            case=case,
            note=note,
            overwrite=overwrite,
        )
    except remote_adoption.RemoteAdoptionError as exc:
        return {"adopted": False, "error": str(exc)}


@mcp.tool()
def set_simulation_note(record_id: str, note: str, confirm: bool = False) -> dict[str, Any]:
    """Write the human-owned note on a simulation. Requires confirmation.

    The note is the one catalog field a person owns; it also round-trips
    through Google Sheets. Only set confirm=True after the user has approved
    the exact wording, since this overwrites whatever was there. The agent's
    own observations go to ``set_ai_note`` instead.
    """
    if not confirm:
        return {
            "written": False,
            "reason": "confirmation required; show the note text and resubmit with confirm=True",
            "record_id": record_id,
            "note": note,
        }
    record = sc.set_note(record_id, note)
    return {"written": True, **sc.summarize(record)}


@mcp.tool()
def set_ai_note(record_id: str, ai_note: str) -> dict[str, Any]:
    """Write the agent's own note on a simulation. No confirmation needed.

    ``ai_note`` is the AI's field, separate from the human ``note``: what the
    run is for, what was found, what to do next. Keep it current -- replace
    the text rather than appending a log -- after a submission, a status
    change worth explaining, or an analysis result. Never put AI-authored
    text in ``note``. The CSV notebook is rewritten so the view stays current;
    the spreadsheet views show this column but never pull it back.
    """
    from tools import notebook_csv

    record = sc.set_ai_note(record_id, ai_note)
    try:
        notebook = notebook_csv.sync(confirm=True)
        notebook_state = {"synced": True, "pulled_notes": notebook.get("pulled_notes", [])}
    except Exception as exc:  # noqa: BLE001 - the note is written either way
        notebook_state = {"synced": False, "error": str(exc)}
    return {"written": True, "notebook": notebook_state, **sc.summarize(record)}


@mcp.tool()
def inspect_input_archive(
    min_number: int = 600,
    max_files: int = 100,
    include_dev: bool = False,
) -> dict[str, Any]:
    """Inspect recent input backups without modifying or validating them.

    Compares parameter names with the newest local XGC metadata cache when
    available. High alignment means only that the schema looks current enough
    to prioritize; it does not make an input a validated baseline.
    ``min_number`` applies to production ``n`` inputs; dev ``d`` inputs are
    included only with ``include_dev``.
    """
    return input_archive.inspect_archive(
        min_number=min_number,
        max_files=max_files,
        include_dev=include_dev,
    )


@mcp.tool()
def rank_promotion_candidates(
    min_number: int = 600,
    max_files: int = 100,
) -> dict[str, Any]:
    """Rank archived inputs for promotion to kb/cases/ baselines.

    Joins the read-only input archive with the researcher's simulation
    notebook (Other_data/simulation_note_XGC1.xlsx), classifying each run's
    recorded outcome from the notebook's own wording and quoting the evidence.
    Recorded successes rank first, then schema alignment, then recency.

    This validates nothing: promotion still requires staging referenced files
    and passing `xgc --dryrun`.
    """
    return cp.rank_promotion_candidates(
        min_number=min_number, max_files=max_files
    )


@mcp.tool()
def plan_case_promotion(archive_name: str) -> dict[str, Any]:
    """Draft a kb case for one archived input. Writes nothing.

    Combines the parsed input, the notebook's intent/outcome for that
    simulation number, and a deterministic physics classification (each field
    cites its source). Lists the remaining steps before the draft can become a
    validated baseline. Show the draft and warnings to the user before calling
    write_draft_case.
    """
    return cp.plan_case_promotion(archive_name)


@mcp.tool()
def write_draft_case(
    archive_name: str,
    confirm: bool = False,
    allow_failed: bool = False,
) -> dict[str, Any]:
    """Write a promotion draft under kb/cases/drafts/. Requires confirmation.

    With confirm=False this returns the plan and writes nothing. Only set
    confirm=True after the user has reviewed the draft. Drafts are invisible
    to list_cases/physics_advisor until a human finalizes them into kb/cases/
    after a passing dryrun. Runs the notebook records as failed are refused
    unless allow_failed=True.
    """
    return cp.write_draft_case(
        archive_name, confirm=confirm, allow_failed=allow_failed
    )


@mcp.tool()
def physics_advisor(
    problem_description: str,
    machine: str | None = None,
) -> dict[str, Any]:
    """Propose a validated XGC baseline from a natural-language physics problem.

    The result is evidence-based: it matches facts and exclusions recorded in
    kb/cases/, never invents numerical values, and carries through only
    explicitly stated values covered by deterministic rules. Unsupported
    physics is reported as unsupported rather than forced onto the closest
    case.

    This tool writes nothing. If it returns ``recommended``, pass its case and
    overrides to plan_input, show the resulting diff to the human, and require
    approval before write_input.
    """
    return pa.advise(problem_description, machine=machine)


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
    simulation_id: str | None = None,
    provenance_class: str | None = None,
    xgc_exe: str | None = None,
    env_script: str | None = None,
    xgc_commit: str | None = None,
    job_name: str | None = None,
    nodes: int | None = None,
    walltime: str | None = None,
    ntasks_per_node: int | None = None,
    cpus_per_task: int | None = None,
    gpus_per_node: int | None = None,
    account: str | None = None,
    partition: str | None = None,
    hello_world: bool = False,
    skip_dryrun: bool | None = None,
    daos: bool = False,
    spare_nodes: int = 0,
    depend_on: str | None = None,
) -> dict[str, Any]:
    """Submit a job to the scheduler. Requires explicit human confirmation.

    With confirm=False (the default) this submits nothing and returns the plan.
    Only set confirm=True after the user has seen the plan and approved it in
    this conversation. Never set it on your own initiative.

    When case_dir is given, `xgc --dryrun` validates the input first and a
    rejected input blocks submission. Always pass case_dir for real XGC runs.
    skip_dryrun defaults to the series: a dev (``d``) simulation skips the
    dryrun, production runs keep it. Leave it unset; pass True for a
    production run only when the user explicitly asks, or False to validate
    a dev run anyway. On machines
    whose login nodes cannot run the dryrun (Polaris, Aurora: dryrun.login_node
    false) the job is submitted first and, if it is still queued after a short
    grace period, a 1-node job runs the dryrun in parallel in the first queue
    that can take it (debug, or a regular queue when the user's debug slot is
    taken); its verdict appears on the next job_status / refresh as
    "dryrun_parallel". skip_dryrun is no longer needed there.

    On success, writes a provenance record below ``runs/master/`` or
    ``runs/test/``. Registered simulations default to master; unregistered and
    hello-world jobs default to test. Pass ``provenance_class="master"`` for an
    intentional unregistered scientific run.

    ``depend_on`` chains this job behind another job id (afterany), which is
    how a restart follows the job still running in the same case directory.
    """
    return hr.submit(
        machine,
        confirm=confirm,
        case_dir=case_dir,
        simulation_id=simulation_id,
        provenance_class=provenance_class,
        xgc_exe=xgc_exe,
        env_script=env_script,
        xgc_commit=xgc_commit,
        skip_dryrun=skip_dryrun,
        job_name=job_name,
        nodes=nodes,
        walltime=walltime,
        ntasks_per_node=ntasks_per_node,
        cpus_per_task=cpus_per_task,
        gpus_per_node=gpus_per_node,
        account=account,
        partition=partition,
        hello_world=hello_world,
        daos=daos,
        spare_nodes=spare_nodes,
        depend_on=depend_on,
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


@mcp.tool()
def analyze_run(
    machine: str,
    run_id: str,
    product: str = "inventory",
    max_entries: int = 1_000,
    max_profile_points: int = 512,
    save: bool = True,
    confirm_overwrite: bool = False,
) -> dict[str, Any]:
    """Generate a bounded run summary remotely; never transfer raw .bp output.

    The first product, ``inventory``, scans only the top-level run directory
    and records ADIOS2 product names plus small file metadata. It does not
    descend into or read any ``.bp`` product. The resulting JSON may be saved
    under the selected run's ``summaries/`` directory for provenance.

    ``oned_profiles`` uses the XGC-Analysis fast catalog remotely to read only
    the first/latest logical steps of selected ``xgc.oneddiag.bp`` profile
    variables. It returns bounded JSON and renders a local SVG; raw BP data
    remains on the HPC machine. ``max_profile_points`` limits radial samples.

    Replacing an existing summary requires ``confirm_overwrite=True``.
    """
    return da.analyze_run(
        machine,
        run_id,
        product=product,
        max_entries=max_entries,
        max_profile_points=max_profile_points,
        save=save,
        confirm_overwrite=confirm_overwrite,
    )


@mcp.tool()
def search_remote_gui_targets(
    query: str,
    machine: str | None = None,
) -> dict[str, Any]:
    """Search local run provenance for a remote XGC_GUI Monitor target."""
    return {"matches": rg.search_provenance(query, machine=machine)}


@mcp.tool()
def plan_remote_gui(
    machine: str,
    remote_directory: str | None = None,
    run_id: str | None = None,
    simulation_id: str | None = None,
    query: str | None = None,
    source: str | None = None,
    preset: str | None = None,
    local_port: int | None = None,
    remote_port: int | None = None,
) -> dict[str, Any]:
    """Resolve and validate a remote XGC_GUI launch without starting it.

    Select exactly one of ``remote_directory``, ``run_id``, ``simulation_id``,
    or ``query``. The check is read-only and uses no scheduler allocation.
    """
    return rg.plan_remote_gui(
        machine,
        remote_directory=remote_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        query=query,
        source=source,
        preset=preset,
        local_port=local_port,
        remote_port=remote_port,
    ).to_dict()


@mcp.tool()
def launch_remote_gui(
    machine: str,
    remote_directory: str | None = None,
    run_id: str | None = None,
    simulation_id: str | None = None,
    query: str | None = None,
    source: str | None = None,
    preset: str | None = None,
    local_port: int | None = None,
    remote_port: int | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Launch XGC-Devel's Monitor remotely and open its local tunnel URL.

    This starts a login-node Flask process and an SSH tunnel only. It never
    submits a scheduler job and never copies or modifies simulation output.
    """
    plan = rg.plan_remote_gui(
        machine,
        remote_directory=remote_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        query=query,
        source=source,
        preset=preset,
        local_port=local_port,
        remote_port=remote_port,
    )
    return rg.launch_remote_gui(plan, open_browser=open_browser)


@mcp.tool()
def remote_gui_status(session_id: str | None = None) -> dict[str, Any]:
    """Show one or all local remote-GUI tunnel sessions."""
    return rg.remote_gui_status(session_id)


@mcp.tool()
def stop_remote_gui(session_id: str) -> dict[str, Any]:
    """Stop one launcher-owned SSH tunnel and its remote GUI process."""
    return rg.stop_remote_gui(session_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
