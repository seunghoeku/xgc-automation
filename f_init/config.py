"""Case definitions, naming conventions, and the per-case log for f_init.

A *case* is one remote root directory (for example
``/pscratch/.../f_init/cbc6``) holding the XGCa run and its dt=0 checks. It
is described by ``f_init/cases/<name>.yaml``; everything the CLI does to it
is appended to ``f_init/cases/<name>.log.yaml`` so a distribution file can
be traced back to the run, step, smoothing, and job that produced it
(absolute rule 5).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tools import machines

CASES_DIR = Path(__file__).resolve().parent / "cases"

# Defaults mirror the manual cbc6 procedure. A case file overrides any of them.
DEFAULTS: dict[str, Any] = {
    "machine": "perlmutter",
    "maxwell_dir": "maxwell",
    "species": {
        "var_string": "i_f",     # variable in xgc.f0.*.bp
        "mass_au": 2.0,
        "charge_num": 1.0,
        "has_electron": False,   # adiabatic electrons -> no e_f in the f0 file
        "slot": 2,               # 1-based position in ptl_f_init_file etc.
    },
    "smooth": {"sigma_theta": 0.0, "v_width": 0.0},
    "dt0": {
        "sml_dt": "2.0D-13",
        "sml_mstep": 20,
        "compare_step": None,    # xgc.2d.<step>.bp of the dt0 run; None = first
    },
    "adjust_factor": 1.0,
    "job": {
        "script": "xgcs.cpu",
        "copy_files": ["adios2cfg.xml", "petsc.rc", "runscript.cpu"],
    },
    "remote": {},                # python / xgc_reader overrides
}


class CaseError(RuntimeError):
    pass


@dataclass
class Case:
    name: str
    root: str
    machine: str
    maxwell_dir: str
    species: dict[str, Any]
    smooth: dict[str, Any]
    dt0: dict[str, Any]
    adjust_factor: float
    job: dict[str, Any]
    remote: dict[str, Any]
    path: Path
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def maxwell_path(self) -> str:
        return f"{self.root.rstrip('/')}/{self.maxwell_dir}"

    def subdir(self, name: str) -> str:
        return f"{self.root.rstrip('/')}/{name}"

    @property
    def helper_dir(self) -> str:
        """Where the login-node scripts are pushed before each run."""
        return f"{self.root.rstrip('/')}/.xgc-finit"

    @property
    def log_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.log.yaml")

    @property
    def artifacts_dir(self) -> Path:
        return self.path.with_name(self.path.stem)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_case_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path)
    if candidate.suffix in {".yaml", ".yml"} and candidate.exists():
        return candidate.resolve()
    path = CASES_DIR / f"{name_or_path}.yaml"
    if not path.exists():
        available = sorted(
            p.stem for p in CASES_DIR.glob("*.yaml") if not p.name.endswith(".log.yaml")
        )
        raise CaseError(
            f"no case {name_or_path!r} under {CASES_DIR} (available: {available}). "
            "Copy example.yaml and fill in the root directory."
        )
    return path


def load_case(name_or_path: str) -> Case:
    path = resolve_case_path(name_or_path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise CaseError(f"{path} must hold a mapping")
    data = _merge(DEFAULTS, raw)
    root = data.get("root")
    if not root or machines.is_placeholder(root):
        raise CaseError(f"{path}: 'root' (remote case directory) is not set")
    known = {"name", "root", "machine", "maxwell_dir", "species", "smooth", "dt0",
             "adjust_factor", "job", "remote", "note"}
    return Case(
        name=str(data.get("name") or path.stem),
        root=str(root),
        machine=str(data["machine"]),
        maxwell_dir=str(data["maxwell_dir"]),
        species=dict(data["species"]),
        smooth=dict(data["smooth"]),
        dt0=dict(data["dt0"]),
        adjust_factor=float(data["adjust_factor"]),
        job=dict(data["job"]),
        remote=dict(data["remote"] or {}),
        path=path,
        note=data.get("note"),
        extra={k: v for k, v in data.items() if k not in known},
    )


def remote_python(case: Case, machine_cfg: dict[str, Any]) -> tuple[str, str]:
    """(python, XGC_reader checkout) for the login node.

    The case file may override; otherwise the machine's ``analysis`` section
    in ``machines.local.yaml`` is used, the same binding ``data_analyzer``
    relies on.
    """
    analysis = machine_cfg.get("analysis") or {}
    python = case.remote.get("python") or analysis.get("python")
    reader = (
        case.remote.get("xgc_reader")
        or analysis.get("xgc_reader")
        or analysis.get("pythonpath")
    )
    missing = [
        label
        for label, value in (("python", python), ("xgc_reader", reader))
        if not value or machines.is_placeholder(value)
    ]
    if missing:
        raise CaseError(
            f"{case.machine}: no login-node {'/'.join(missing)} configured. Set "
            "analysis.python and analysis.pythonpath (XGC_reader checkout) in "
            "machines.local.yaml, or remote.python / remote.xgc_reader in the case file."
        )
    return str(python), str(reader)


# --- naming --------------------------------------------------------------

def fmt_num(value: float | int) -> str:
    """``0.3 -> '0.3'``, ``1.0 -> '1'``, ``0 -> '0'`` -- the manual convention."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


def smooth_tag(sigma_theta: float, v_width: float) -> str:
    return f"{fmt_num(sigma_theta)}_{fmt_num(v_width)}"


NO_SMOOTH_FILE = "xgc.f_init_no_smooth.bp"


def smooth_file(sigma_theta: float, v_width: float) -> str:
    return f"xgc.f_init_smooth_{smooth_tag(sigma_theta, v_width)}.bp"


def finit_tag(filename: str) -> str:
    """``xgc.f_init_smooth_0.3_0.5.bp -> smooth_0.3_0.5``."""
    base = Path(filename).name
    match = re.match(r"^xgc\.f_init_(.+)\.bp$", base)
    if not match:
        raise CaseError(
            f"{base!r} does not follow xgc.f_init_<tag>.bp; cannot derive a tag"
        )
    return match.group(1)


def dt0_dirname(finit_filename: str) -> str:
    return f"dt0_{finit_tag(finit_filename)}"


def adjusted_file(finit_filename: str) -> str:
    """Next adjusted name: ``no_smooth -> adjusted_no_smooth -> adjusted2_no_smooth``."""
    tag = finit_tag(finit_filename)
    match = re.match(r"^adjusted(\d*)_(.+)$", tag)
    if match:
        n = int(match.group(1) or "1") + 1
        return f"xgc.f_init_adjusted{n}_{match.group(2)}.bp"
    return f"xgc.f_init_adjusted_{tag}.bp"


# --- log -----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_log(case: Case) -> list[dict[str, Any]]:
    if not case.log_path.exists():
        return []
    with case.log_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return list(entries or [])


def append_log(case: Case, stage: str, **fields: Any) -> dict[str, Any]:
    entry = {"time": _now(), "stage": stage, **fields}
    entries = read_log(case)
    entries.append(entry)
    with case.log_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            {"case": case.name, "root": case.root, "entries": entries},
            fh, sort_keys=False, allow_unicode=False,
        )
    return entry


def update_log_entry(case: Case, match: dict[str, Any], **fields: Any) -> bool:
    """Set fields on the newest entry whose keys all equal ``match``."""
    entries = read_log(case)
    for entry in reversed(entries):
        if all(entry.get(k) == v for k, v in match.items()):
            entry.update(fields)
            with case.log_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(
                    {"case": case.name, "root": case.root, "entries": entries},
                    fh, sort_keys=False, allow_unicode=False,
                )
            return True
    return False
