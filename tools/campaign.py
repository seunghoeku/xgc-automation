"""Package a finished simulation as an HPC-Campaign archive (``.aca``).

XGC writes plain ADIOS2 ``.bp`` products; it knows nothing about campaigns.
The archive is made afterwards by XGC-Analysis's packager
(``python -m xgc_analysis.catalog.package_xgc_run``), which scans the run
directory, writes a manifest, and drives ``hpc_campaign manager`` to add the
BP data, the file-sequence time series, and the text artifacts. This module
decides *when* that happens and keeps the result in the catalog:

    simulation turns ``completed``  ->  packager starts on the login node
    later refresh / status check    ->  result collected into ``campaign``

Placement follows docs/data-analysis.md: packaging reads BP metadata and
writes one SQLite file, so it is file-access dominant and runs on the login
node, detached, under ``timeout`` at the six-hour login-node limit. The SSH
call that starts it returns at once; nothing here waits for it.

Layout in the remote run directory::

    <run_dir>/<simulation_id>.aca            the archive, only once complete
    <run_dir>/.xgc-campaign/
        <simulation_id>.aca                  archive being built
        xgc_run_manifest.json                packager manifest
        build.log                            packager stdout/stderr
        result.json                          {"exit_code", "finished_at"}
        lock/                                present while a build runs

The archive is built inside ``.xgc-campaign/`` and moved into place only on
success, so a file named ``<simulation_id>.aca`` in the run directory is
always a finished archive.

Safety (AGENTS.md rule 1): an automatic build never replaces an existing
archive. When a simulation completes again after a restart, the old archive
is reported as stale and replacing it needs ``replace=True`` with
``confirm=True`` after the user agreed.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import machines as m
from tools import simulation_catalog as catalog
from tools import ssh

# The login-node ceiling from docs/data-analysis.md
# (data_analyzer.LOGIN_NODE_MAX_RUNTIME_SECONDS). A configured timeout may be
# shorter, never longer.
LOGIN_NODE_MAX_SECONDS = 6 * 60 * 60
WORK_DIRNAME = ".xgc-campaign"
DEFAULT_ARCHIVE_NAME = "{simulation_id}.aca"
# Simulation kinds packaged automatically unless `campaign.auto_kinds` says
# otherwise. Dev runs are short tests; package one by hand if it matters.
DEFAULT_AUTO_KINDS = ("production",)

# `pending`: wanted but not started (machine unreachable, environment
# missing); retried by every refresh. `building`: started, no result yet.
# `built` / `failed`: the packager's outcome.
STATES = ("pending", "building", "built", "failed")
RETRY_STATES = ("pending", "building")

# Margin past the packager timeout before a build with no result file is
# declared lost (login node rebooted, process killed).
_LOST_MARGIN_SECONDS = 30 * 60

_MARK = "XGC_CAMPAIGN"


class CampaignError(RuntimeError):
    """A campaign archive cannot be planned or started."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# -- configuration ---------------------------------------------------------


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve a machine's campaign settings.

    ``campaign.python`` / ``pythonpath`` / ``env_script`` fall back to the
    machine's ``analysis`` runtime, since both need ``xgc_analysis``; the
    campaign interpreter must additionally import ``hpc_campaign``.
    """
    section = cfg.get("campaign") or {}
    if not isinstance(section, dict):
        raise CampaignError("campaign must be a mapping in machines.yaml")
    analysis = cfg.get("analysis") or {}

    python = section.get("python") or analysis.get("python")
    pythonpath = section.get("pythonpath", analysis.get("pythonpath"))
    if isinstance(pythonpath, list):
        pythonpath = ":".join(str(item) for item in pythonpath)
    env_script = section.get("env_script")
    if env_script is None:
        env_script = analysis.get("env_script")
    if env_script is None:
        env_script = (cfg.get("xgc") or {}).get("env_script")

    try:
        timeout = int(section.get("timeout_seconds") or LOGIN_NODE_MAX_SECONDS)
    except (TypeError, ValueError):
        raise CampaignError("campaign.timeout_seconds must be an integer")
    timeout = max(60, min(timeout, LOGIN_NODE_MAX_SECONDS))

    kinds = section.get("auto_kinds") or list(DEFAULT_AUTO_KINDS)
    if isinstance(kinds, str):
        kinds = [kinds]

    return {
        "auto": bool(section.get("auto", False)),
        "auto_kinds": [str(kind) for kind in kinds],
        "python": python,
        "pythonpath": pythonpath or None,
        "env_script": env_script or None,
        "hostname": section.get("hostname") or None,
        "archive_name": str(section.get("archive_name") or DEFAULT_ARCHIVE_NAME),
        "timeout_seconds": timeout,
    }


def _check_settings(resolved: dict[str, Any]) -> None:
    python = resolved["python"]
    if not isinstance(python, str) or not python or m.is_placeholder(python):
        raise CampaignError(
            "campaign.python (or analysis.python) must name a remote Python "
            "that can import xgc_analysis and hpc_campaign"
        )
    if not resolved["hostname"]:
        # Without it hpc_campaign records the login node's FQDN as the data's
        # host, and ADIOS then treats the data as remote even on that same
        # machine: variables list, reads fail (EVPathRemoteFile FileNotOpen;
        # verified on Polaris and Aurora 2026-09-24).
        raise CampaignError(
            "campaign.hostname is not set; choose the HPC-Campaign host alias "
            "for this machine's data (the same name in the readers' hosts.yaml)"
        )
    for key in ("pythonpath", "env_script", "hostname"):
        value = resolved[key]
        if value is not None and (not isinstance(value, str) or m.is_placeholder(value)):
            raise CampaignError(f"campaign.{key} is not filled in")


# -- which run, which directory ----------------------------------------------


def _latest_completed_run(record: dict[str, Any]) -> dict[str, Any] | None:
    runs = [run for run in record.get("runs") or [] if isinstance(run, dict)]
    ordered = sorted(
        runs,
        key=lambda run: (str(run.get("submitted_at") or ""), str(run.get("run_id") or "")),
    )
    for run in reversed(ordered):
        if run.get("state") == "completed":
            return run
    return None


def plan(record_id: str, *, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Work out what a build would do for one simulation. Contacts nothing."""
    record = catalog.read_record(record_id, catalog_dir=catalog_dir)
    run = _latest_completed_run(record)
    if run is None:
        raise CampaignError(f"{record_id} has no completed run to package")
    machine = str(run.get("machine") or "")
    if not machine:
        raise CampaignError(f"the completed run of {record_id} names no machine")
    run_dir = run.get("remote_run_dir") or (record.get("remote_dirs") or {}).get(machine)
    if not run_dir:
        raise CampaignError(f"{record_id} has no remote run directory on {machine}")
    run_dir = str(run_dir).rstrip("/")

    resolved = settings(m.get_machine(machine))
    archive_name = resolved["archive_name"].format(simulation_id=record_id)
    if "/" in archive_name or not archive_name.endswith(".aca"):
        raise CampaignError("campaign.archive_name must be a plain file name ending in .aca")
    work_dir = f"{run_dir}/{WORK_DIRNAME}"
    return {
        "record_id": record_id,
        "kind": catalog.record_kind(record),
        "status": record.get("status"),
        "machine": machine,
        "for_run_id": run.get("run_id"),
        "run_dir": run_dir,
        "archive": f"{run_dir}/{archive_name}",
        "work_dir": work_dir,
        "partial": f"{work_dir}/{archive_name}",
        "manifest": f"{work_dir}/xgc_run_manifest.json",
        "log": f"{work_dir}/build.log",
        "result": f"{work_dir}/result.json",
        "settings": resolved,
        "current": record.get("campaign"),
    }


# -- remote scripts ------------------------------------------------------------


def _packager_argv(p: dict[str, Any]) -> list[str]:
    resolved = p["settings"]
    argv = [
        "timeout",
        str(resolved["timeout_seconds"]),
        resolved["python"],
        "-m",
        "xgc_analysis.catalog.package_xgc_run",
        p["run_dir"],
        "--archive",
        p["partial"],
        "--manifest",
        p["manifest"],
        "--run-id",
        p["record_id"],
        # Applies to the private in-progress file only, so a retry after a
        # failed build starts clean instead of appending to its leftovers.
        "--truncate",
    ]
    if resolved["hostname"]:
        argv += ["--hostname", resolved["hostname"]]
    if resolved["pythonpath"]:
        argv = ["env", f"PYTHONPATH={resolved['pythonpath']}", *argv]
    return argv


def _start_script(p: dict[str, Any], *, replace: bool) -> str:
    """Shell run over SSH: checks, then detaches the packager and returns."""
    resolved = p["settings"]
    q = shlex.quote
    python_check = [resolved["python"], "-c", "import xgc_analysis.catalog.package_xgc_run, hpc_campaign"]
    if resolved["pythonpath"]:
        python_check = ["env", f"PYTHONPATH={resolved['pythonpath']}", *python_check]
    source = (
        f"source {q(resolved['env_script'])} >/dev/null 2>&1 || "
        f"{{ echo '{_MARK}_ENV env_script failed'; exit 6; }}"
        if resolved["env_script"]
        else ":"
    )
    stale_minutes = (resolved["timeout_seconds"] + _LOST_MARGIN_SECONDS) // 60
    # The worker inherits the environment the checks below sourced.
    worker = "\n".join(
        [
            f"cd {q(p['run_dir'])}",
            shlex.join(_packager_argv(p)),
            "rc=$?",
            f"if [ $rc -eq 0 ]; then mv -f {q(p['partial'])} {q(p['archive'])} || rc=$?; fi",
            "printf '{\"exit_code\": %d, \"finished_at\": \"%s\"}\\n' "
            "\"$rc\" \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" "
            f"> {q(p['result'])}.tmp && mv -f {q(p['result'])}.tmp {q(p['result'])}",
            f"rmdir {q(p['work_dir'])}/lock 2>/dev/null",
        ]
    )
    lines = [
        f"run_dir={q(p['run_dir'])}",
        f"work={q(p['work_dir'])}",
        f"[ -d \"$run_dir\" ] || {{ echo '{_MARK}_ERROR run directory not found'; exit 3; }}",
    ]
    if not replace:
        lines.append(
            f"[ -e {q(p['archive'])} ] && {{ echo '{_MARK}_EXISTS'; exit 4; }}"
        )
    lines += [
        source,
        f"{shlex.join(python_check)} >/dev/null 2>&1 || "
        f"{{ echo '{_MARK}_ENV cannot import xgc_analysis.catalog.package_xgc_run and hpc_campaign with {resolved['python']}'; exit 5; }}",
        'mkdir -p "$work" || exit 7',
        # A lock older than the timeout plus margin belongs to a build that
        # died without cleaning up.
        f'find "$work/lock" -maxdepth 0 -mmin +{stale_minutes} -exec rmdir {{}} \\; 2>/dev/null',
        f"mkdir \"$work/lock\" 2>/dev/null || {{ echo '{_MARK}_BUSY'; exit 8; }}",
        'rm -f "$work/result.json"',
        # setsid detaches from the SSH session's process group where it exists.
        "detach=; command -v setsid >/dev/null 2>&1 && detach=setsid",
        f"nohup $detach bash -c {q(worker)} > {q(p['log'])} 2>&1 < /dev/null &",
        f"echo \"{_MARK}_STARTED $(hostname)\"",
    ]
    return "\n".join(lines)


def _poll_script(p: dict[str, Any]) -> str:
    q = shlex.quote
    return "\n".join(
        [
            f"if [ -f {q(p['result'])} ]; then echo '{_MARK}_RESULT'; cat {q(p['result'])};",
            f"else echo '{_MARK}_RUNNING'; fi",
            f"[ -f {q(p['archive'])} ] && echo \"{_MARK}_SIZE "
            f"$(stat -c %s {q(p['archive'])} 2>/dev/null || stat -f %z {q(p['archive'])})\"",
            f"echo '{_MARK}_LOG'",
            f"tail -n 20 {q(p['log'])} 2>/dev/null",
            "true",
        ]
    )


def _marker(stdout: str, name: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith(f"{_MARK}_{name}"):
            return line[len(_MARK) + 1 + len(name):].strip()
    return None


# -- state in the catalog ---------------------------------------------------------


def _entry(p: dict[str, Any], **fields: Any) -> dict[str, Any]:
    entry = {
        "state": None,
        "machine": p["machine"],
        "for_run_id": p["for_run_id"],
        "archive": p["archive"],
        "log": p["log"],
        "hostname": p["settings"]["hostname"],
    }
    entry.update(fields)
    return entry


def _store(record_id: str, entry: dict[str, Any], catalog_dir: Path | None) -> None:
    catalog.update_record(record_id, {"campaign": entry}, catalog_dir=catalog_dir)


def _start(
    p: dict[str, Any],
    *,
    replace: bool,
    catalog_dir: Path | None,
) -> dict[str, Any]:
    """Start the packager and record what happened. Never raises for SSH."""
    record_id = p["record_id"]
    try:
        _check_settings(p["settings"])
    except CampaignError as exc:
        entry = _entry(p, state="pending", reason=str(exc), checked_at=_now())
        _store(record_id, entry, catalog_dir)
        return {"record_id": record_id, "started": False, **entry}

    host = ssh.host_for(m.get_machine(p["machine"]))
    try:
        result = ssh.run(host, ["bash", "-c", _start_script(p, replace=replace)], timeout=120)
    except ssh.SSHError as exc:
        entry = _entry(p, state="pending", reason=str(exc), checked_at=_now())
        _store(record_id, entry, catalog_dir)
        return {"record_id": record_id, "started": False, **entry}

    out = result.stdout
    if _marker(out, "STARTED") is not None:
        entry = _entry(
            p,
            state="building",
            started_at=_now(),
            login_node=_marker(out, "STARTED"),
            replace=bool(replace),
        )
    elif _marker(out, "EXISTS") is not None:
        # An archive already sits at the destination and is left alone.
        current = p.get("current") if isinstance(p.get("current"), dict) else {}
        if current.get("state") == "built":
            entry = dict(current)
            if current.get("for_run_id") != p["for_run_id"]:
                entry["stale"] = True
                entry["reason"] = (
                    f"built for run {current.get('for_run_id')}, but run "
                    f"{p['for_run_id']} completed since; rebuild with replace=True "
                    "after confirmation"
                )
        else:
            # Not made by this tool (or its record was lost). `failed` stops
            # the retries; only a confirmed replace touches it.
            entry = _entry(
                p,
                state="failed",
                reason=(
                    f"{p['archive']} already exists and was not recorded as built "
                    "here; replacing it needs replace=True and the user's confirmation"
                ),
                checked_at=_now(),
            )
    elif _marker(out, "BUSY") is not None:
        entry = _entry(
            p,
            state="building",
            reason="a campaign build is already running in this directory",
            started_at=(p.get("current") or {}).get("started_at") or _now(),
        )
    elif _marker(out, "ENV") is not None:
        # Recoverable by installing the packages: keep retrying.
        entry = _entry(p, state="pending", reason=_marker(out, "ENV"), checked_at=_now())
    else:
        detail = (_marker(out, "ERROR") or result.stderr.strip() or out.strip())[-500:]
        entry = _entry(
            p,
            state="failed",
            reason=f"could not start the packager (rc={result.returncode}): {detail}",
            checked_at=_now(),
        )
    _store(record_id, entry, catalog_dir)
    return {"record_id": record_id, "started": entry["state"] == "building", **entry}


def _poll(p: dict[str, Any], *, catalog_dir: Path | None) -> dict[str, Any]:
    """Collect a running build's outcome, if it has one."""
    record_id = p["record_id"]
    current = dict(p.get("current") or {})
    host = ssh.host_for(m.get_machine(p["machine"]))
    try:
        result = ssh.run(host, ["bash", "-c", _poll_script(p)], timeout=60)
    except ssh.SSHError as exc:
        return {"record_id": record_id, "state": "building", "reason": str(exc), "retry": True}

    out = result.stdout
    log_tail = out.split(f"{_MARK}_LOG", 1)[1].strip() if f"{_MARK}_LOG" in out else ""
    if f"{_MARK}_RESULT" in out:
        body = out.split(f"{_MARK}_RESULT", 1)[1].split(f"{_MARK}_", 1)[0]
        try:
            outcome = json.loads(body.strip())
        except json.JSONDecodeError:
            outcome = {"exit_code": None}
        exit_code = outcome.get("exit_code")
        current.update(
            {
                "state": "built" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "finished_at": outcome.get("finished_at") or _now(),
            }
        )
        current.pop("reason", None)
        current.pop("stale", None)
        size = _marker(out, "SIZE")
        if exit_code == 0 and size and size.isdigit():
            current["size_bytes"] = int(size)
        if exit_code != 0:
            reason = "packager timed out" if exit_code == 124 else f"packager exited {exit_code}"
            current["reason"] = f"{reason}; log tail: {log_tail[-800:]}"
        _store(record_id, current, catalog_dir)
        return {"record_id": record_id, **current}

    started = _parse_time(current.get("started_at"))
    limit = p["settings"]["timeout_seconds"] + _LOST_MARGIN_SECONDS
    if started and (datetime.now(timezone.utc) - started).total_seconds() > limit:
        current.update(
            {
                "state": "failed",
                "reason": "no result after the packager timeout; the login-node process was lost",
                "finished_at": _now(),
            }
        )
        _store(record_id, current, catalog_dir)
        return {"record_id": record_id, **current}
    return {"record_id": record_id, "state": "building", "log_tail": log_tail[-400:]}


# -- entry points --------------------------------------------------------------------


def on_completion(
    record_id: str,
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Start a campaign build for a simulation that has just completed.

    Called by refresh and by job status checks after a state change. Returns
    None when nothing applies: not completed, automatic packaging off for the
    machine or kind, or an archive for this run already built or building.
    Never raises: packaging must not break a status query.
    """
    try:
        p = plan(record_id, catalog_dir=catalog_dir)
    except (CampaignError, catalog.SimulationCatalogError, KeyError):
        return None
    if p["status"] != "completed":
        return None
    resolved = p["settings"]
    if not resolved["auto"] or p["kind"] not in resolved["auto_kinds"]:
        return None
    current = p["current"] if isinstance(p["current"], dict) else {}
    if current.get("for_run_id") == p["for_run_id"] and current.get("state") in (
        "building",
        "built",
        "failed",
    ):
        # Already handled for this run; a failure is retried only by hand.
        return None
    if current.get("state") == "built" and current.get("for_run_id") != p["for_run_id"]:
        # A later run completed after the archive was made. Replacing it is an
        # overwrite, which the user approves.
        entry = dict(current)
        entry["stale"] = True
        entry["reason"] = (
            f"run {p['for_run_id']} completed after this archive was built for "
            f"{current.get('for_run_id')}; rebuild with replace=True after confirmation"
        )
        try:
            _store(record_id, entry, catalog_dir)
        except catalog.SimulationCatalogError:
            return None
        return {"record_id": record_id, "started": False, **entry}
    try:
        return _start(p, replace=False, catalog_dir=catalog_dir)
    except (CampaignError, catalog.SimulationCatalogError, KeyError) as exc:
        return {"record_id": record_id, "started": False, "error": str(exc)}


def follow_up(
    *,
    catalog_dir: Path | None = None,
    machine: str | None = None,
    record_id: str | None = None,
    skip: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Retry pending builds and collect finished ones, for every record.

    Run at the end of every refresh. Costs one SSH call per record in a
    retry state and nothing otherwise.
    """
    results: list[dict[str, Any]] = []
    for record in catalog.iter_records(catalog_dir=catalog_dir):
        rid = str(record.get("record_id") or "")
        if (record_id and rid != record_id) or (skip and rid in skip):
            continue
        current = record.get("campaign")
        if not isinstance(current, dict) or current.get("state") not in RETRY_STATES:
            continue
        if current.get("stale") and current.get("state") != "building":
            continue
        if machine and current.get("machine") != machine:
            continue
        try:
            p = plan(rid, catalog_dir=catalog_dir)
            if current.get("state") == "building":
                results.append(_poll(p, catalog_dir=catalog_dir))
            elif p["status"] == "completed":
                results.append(_start(p, replace=False, catalog_dir=catalog_dir))
        except (CampaignError, catalog.SimulationCatalogError, KeyError) as exc:
            results.append({"record_id": rid, "error": str(exc)})
    return results


def build(
    record_id: str,
    *,
    confirm: bool = False,
    replace: bool = False,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Build (or rebuild) one simulation's archive by hand.

    Without ``confirm`` this returns the plan and the exact packager command
    and contacts nothing. ``replace=True`` overwrites an existing archive at
    the destination, so only pass it with ``confirm=True`` after the user
    agreed. Works for any kind and regardless of ``campaign.auto``.
    """
    p = plan(record_id, catalog_dir=catalog_dir)
    current = p["current"] if isinstance(p["current"], dict) else {}
    if current.get("state") == "building" and not replace:
        return {"record_id": record_id, "started": False, **current,
                "reason": "a build is already running; check it with campaign status"}
    if not confirm:
        return {
            "record_id": record_id,
            "started": False,
            "confirm_required": True,
            "machine": p["machine"],
            "run_dir": p["run_dir"],
            "archive": p["archive"],
            "for_run_id": p["for_run_id"],
            "replace": bool(replace),
            "command": shlex.join(_packager_argv(p)),
            "current": current or None,
            "reason": "nothing started; resubmit with confirm=True"
            + (" (replace=True overwrites the existing archive)" if replace else ""),
        }
    return _start(p, replace=replace, catalog_dir=catalog_dir)


def status(record_id: str, *, catalog_dir: Path | None = None) -> dict[str, Any]:
    """Report a simulation's campaign state, collecting a running build's result."""
    record = catalog.read_record(record_id, catalog_dir=catalog_dir)
    current = record.get("campaign")
    if not isinstance(current, dict):
        return {"record_id": record_id, "state": None, "reason": "no campaign build recorded"}
    if current.get("state") != "building":
        return {"record_id": record_id, **current}
    return _poll(plan(record_id, catalog_dir=catalog_dir), catalog_dir=catalog_dir)
