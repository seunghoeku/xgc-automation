"""The study archive: one HPC-Campaign ``.aca`` per case, updated every iteration.

The archive is the ledger of the search (docs/campaign.md, "flux as a
reader-only hub"). It is built where the data is (the compute machine's
login node, with that machine's hpc-campaign venv) and only the small
``.aca`` file travels to the controller. Per iteration it receives

- text, embedded in the archive and readable anywhere without a connector:
  ``<label>/input``, ``<label>/omega_input.prf`` (the rotation profile the
  run used), ``<label>/midplane.json`` and ``<label>/midplane.prf`` (the
  analysis result);
- data, referenced by host alias and read remotely through the campaign
  connector: ``<label>/xgc.oneddiag.bp``, the geometry files and the
  ``xgc.f2d`` (or f3d) steps of the averaging window.

``adjust`` reads the analysis of the previous iteration from the archive
copy on the controller, so the loop state lives in the ``.aca``, not in any
process: a controller that lost its session, or another host with the same
archive, continues from the same point.

Placement: ``hpc_campaign manager`` only reads ADIOS metadata and copies
text, so it runs on the login node, bounded by ``timeout``.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from tools import ssh

from . import config as cfg


class CampaignError(RuntimeError):
    """The study archive cannot be updated or read."""


DEFAULT_DATA = ["xgc.oneddiag.bp", "xgc.mesh.bp", "xgc.equil.bp", "xgc.bfield.bp"]


def settings(case: cfg.Case) -> dict[str, Any]:
    c = dict(case.data.get("campaign") or {})
    c.setdefault("enabled", False)
    c.setdefault("archive", "{case}.aca")
    c.setdefault("local_store", "~/hpc-campaign/store/flow_match")
    c.setdefault("data", list(DEFAULT_DATA))
    c.setdefault("window_files", True)
    c.setdefault("timeout", 3600)
    return c


def enabled(case: cfg.Case) -> bool:
    return bool(settings(case).get("enabled"))


def archive_name(case: cfg.Case) -> str:
    return str(settings(case)["archive"]).format(case=case.name)


def remote_archive(case: cfg.Case, machine: str) -> str:
    c = settings(case)
    stores = c.get("remote_store") or {}
    store = stores.get(machine)
    if not store:
        raise CampaignError(f"campaign.remote_store has no entry for {machine!r}")
    return f"{store}/{archive_name(case)}"


def local_archive(case: cfg.Case) -> Path:
    store = Path(os.path.expanduser(str(settings(case)["local_store"])))
    return store / archive_name(case)


def _manager_argv(case: cfg.Case, machine_cfg: dict[str, Any], archive: str, *cmd: str) -> list[str]:
    python, _ = cfg.remote_python(case, machine_cfg)
    exe = str(Path(python).parent / "hpc_campaign")
    argv = [exe, "manager", archive]
    hostname = ((machine_cfg.get("campaign") or {}).get("hostname")) or (settings(case).get("hostname") or {}).get(machine_cfg.get("name", ""))
    if hostname:
        argv += ["--hostname", str(hostname)]
    argv += list(cmd)
    return argv


def _run(target: ssh.SSHTarget, argv: list[str], timeout: float) -> str:
    result = ssh.run(target, argv, timeout=timeout)
    if not result.ok:
        tail = "\n".join(((result.stdout or "") + "\n" + (result.stderr or "")).splitlines()[-15:])
        raise CampaignError(f"{shlex.join(argv[:4])} ... failed (rc={result.returncode}):\n{tail}")
    return result.stdout or ""


def update_archive(
    case: cfg.Case,
    label: str,
    *,
    machine: str,
    machine_cfg: dict[str, Any],
    target: ssh.SSHTarget,
    run_dir: str,
    analysis_stem: str,
    window: dict[str, Any] | None,
    diag_prefix: str = "f2d",
    input_prf_remote: str | None = None,
) -> dict[str, Any]:
    """Add iteration ``label`` to the study archive on ``machine`` and pull the ``.aca``."""
    c = settings(case)
    timeout = float(c.get("timeout", 3600))
    archive = remote_archive(case, machine)
    ssh.mkdir_p(target, str(Path(archive).parent))
    added: dict[str, list[str]] = {"text": [], "data": []}

    def add(kind: str, remote_file: str, name: str) -> None:
        if not ssh.exists(target, remote_file):
            return
        extra = ["--store"] if kind == "text" else []  # embed the text; without it only a remote reference is recorded
        _run(target, _manager_argv(case, machine_cfg, archive, kind, "--name", name, *extra, remote_file), timeout)
        added[kind].append(name)

    add("text", f"{run_dir}/input", f"{label}/input")
    if input_prf_remote:
        add("text", input_prf_remote, f"{label}/omega_input.prf")
    add("text", f"{analysis_stem}.json", f"{label}/midplane.json")
    add("text", f"{analysis_stem}.prf", f"{label}/midplane.prf")
    for fname in c.get("data") or []:
        add("data", f"{run_dir}/{fname}", f"{label}/{fname}")
    if c.get("window_files") and window:
        steps = _window_steps(target, run_dir, diag_prefix, int(window["step_min"]), int(window["step_max"]))
        for step in steps:
            fname = f"xgc.{diag_prefix}.{step:05d}.bp"
            add("data", f"{run_dir}/{fname}", f"{label}/{fname}")

    local = local_archive(case)
    local.parent.mkdir(parents=True, exist_ok=True)
    ssh.pull(target, archive, str(local))
    return {"remote": archive, "local": str(local), "added": added, "machine": machine}


def _window_steps(target: ssh.SSHTarget, run_dir: str, prefix: str, lo: int, hi: int) -> list[int]:
    # argv is quoted on the remote side, so a shell glob would not expand; find does the matching.
    result = ssh.run(target, ["find", run_dir, "-maxdepth", "1", "-name", f"xgc.{prefix}.*.bp"], timeout=60.0)
    steps = []
    for line in (result.stdout or "").split():
        base = os.path.basename(line.strip())
        try:
            step = int(base.split(".")[2])
        except (IndexError, ValueError):
            continue
        if lo <= step <= hi:
            steps.append(step)
    return sorted(steps)


# --- reading on the controller ---------------------------------------------


def _text_rows(path: Path):
    """(dataset name, file row) of every embedded TEXT dataset, straight from SQLite.

    Layout (hpc-campaign 0.7): dataset -> replica (rowid = datasetid) ->
    repfiles -> file, where ``file.data`` holds the zlib-compressed bytes when
    ``compression`` is set. Reading this way needs neither ``hpc_campaign``
    nor ``xgc_analysis`` on the controller.
    """
    import sqlite3

    con = sqlite3.connect(str(path))
    try:
        rows = con.execute(
            """select d.name, f.compression, f.data from dataset d
               join replica r on r.datasetid = d.rowid
               join repfiles rf on rf.replicaid = r.rowid
               join file f on f.fileid = rf.fileid
               where d.fileformat = 'TEXT' and d.deltime = 0"""
        ).fetchall()
    finally:
        con.close()
    return rows


def text_names(case: cfg.Case) -> list[str]:
    path = local_archive(case)
    if not path.exists():
        raise CampaignError(f"no local archive copy: {path}")
    return sorted({name for name, _c, _d in _text_rows(path)})


def read_text(case: cfg.Case, name: str) -> str:
    import zlib

    path = local_archive(case)
    if not path.exists():
        raise CampaignError(f"no local archive copy: {path}")
    for dname, comp, data in _text_rows(path):
        if dname == name:
            raw = zlib.decompress(data) if comp else data
            return raw.decode("utf-8")
    raise CampaignError(f"{name} is not an embedded text artifact of {path}")


def read_midplane(case: cfg.Case, label: str) -> dict[str, Any]:
    """The analysis payload of ``label`` as stored in the archive."""
    return json.loads(read_text(case, f"{label}/midplane.json"))


def iterations(case: cfg.Case) -> list[str]:
    labels = sorted({n.split("/")[0] for n in text_names(case) if n.endswith("/midplane.json")})
    return labels


def verify_remote_read(case: cfg.Case, label: str, variable: str = "time") -> dict[str, Any]:
    """Read one value of ``<label>/xgc.oneddiag.bp`` through the campaign connector.

    Proves that this host reaches the data machine by alias (docs/campaign.md).
    """
    import adios2

    path = local_archive(case)
    name = f"{label}/xgc.oneddiag.bp/{variable}"
    with adios2.FileReader(str(path)) as f:
        names = f.available_variables()
        if name not in names:
            candidates = sorted(n for n in names if n.startswith(f"{label}/"))[:10]
            raise CampaignError(f"{name} not in archive; entries for {label}: {candidates}")
        value = f.read(name)
    return {"variable": name, "shape": list(getattr(value, "shape", [])), "first": float(value.ravel()[0]) if getattr(value, "size", 0) else None}
