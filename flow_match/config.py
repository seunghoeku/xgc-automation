"""Case files and the iteration log of the flow-matching procedure.

A case is ``flow_match/cases/<name>.yaml`` plus a directory
``flow_match/cases/<name>/`` that holds the input template, the solver
files copied into every run, the target profile and the starting rotation
profile. Everything the procedure produces (analysis JSON/PNG, adjusted
profiles) goes to ``flow_match/cases/<name>/iterations/<label>/`` so a case
directory is a complete record of one search.

The log ``flow_match/cases/<name>.log.yaml`` is append-only in spirit: one
entry per action (analyze, adjust, stage, submit, status), the newest last.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tools import paths  # noqa: E402

PACKAGE_DIR = Path(__file__).resolve().parent
# Case logs are per-user state and follow XGC_CATALOG_DIR (default: this package).
CASES_DIR = paths.FLOW_MATCH_CASES_DIR

DEFAULTS: dict[str, Any] = {
    "machine": "perlmutter",
    "backend": "automation",  # automation (catalog + hpc_runner) | direct (ssh + sbatch only)
    "kind": "production",
    "short_name": "flow_match",
    "template": "input",
    "copy_files": ["adios2cfg.xml", "petsc.rc"],
    "input_file_dir": "../XGC_inputs/",
    "flow": {
        "param": "eq_flow_file",
        "slots": [1, 2, 3],
        "initial": None,
        "prefix": "omg_flow_match",
    },
    "target": {
        "kind": "vtor",  # measured toroidal velocity file (km/s by default)
        "file": None,
        "scale": 1.0e3,
        "psi_max": 1.0,
    },
    "species": {"f3d_var": "i2u_para", "label": "carbon"},
    "analysis": {
        "last": 500,
        "nr": 400,
        "probes": [0.3, 0.6, 0.8, 0.9, 0.95],
        "saturation_tol": 0.05,
    },
    "adjust": {
        "relax": 1.0,
        "sol": {"psi_start": 1.0, "mode": "follow", "width": 0.05, "fit_width": 0.05, "taper": 0.0, "value": 0.0},
        "smooth_delta": {"window": 31, "order": 5},
        "smooth_new": {"window": 11, "order": 5},
        "psi_min": 0.0,
    },
    "job": {},
    "remote": {},
    "mstep": None,
    "overrides": {},
    "case_dir": None,
}


class CaseError(RuntimeError):
    """A case file is missing or inconsistent."""


@dataclass
class Case:
    name: str
    path: Path
    data: dict[str, Any]

    @property
    def machine(self) -> str:
        return str(self.data["machine"])

    @property
    def backend(self) -> str:
        return str(self.data.get("backend") or "automation")

    @property
    def kind(self) -> str:
        return str(self.data["kind"])

    @property
    def short_name(self) -> str:
        return str(self.data["short_name"])

    @property
    def case_dir(self) -> Path:
        """Directory with template, solver files and profiles. ``case_dir:`` lets a
        variant (a test case) reuse another case's directory."""
        override = self.data.get("case_dir")
        if override:
            path = Path(str(override)).expanduser()
            return path if path.is_absolute() else (self.path.parent / path)
        return self.path.with_suffix("")

    @property
    def overrides(self) -> dict[str, str]:
        """Extra namelist values (Fortran-rendered strings) applied to every input."""
        return {str(k): str(v) for k, v in (self.data.get("overrides") or {}).items()}

    @property
    def template_path(self) -> Path:
        return self.case_dir / str(self.data["template"])

    @property
    def copy_files(self) -> list[str]:
        return list(self.data.get("copy_files") or [])

    @property
    def flow(self) -> dict[str, Any]:
        return self.data["flow"]

    @property
    def target(self) -> dict[str, Any]:
        return self.data["target"]

    @property
    def species(self) -> dict[str, Any]:
        return self.data["species"]

    @property
    def analysis(self) -> dict[str, Any]:
        return self.data["analysis"]

    @property
    def adjust(self) -> dict[str, Any]:
        return self.data["adjust"]

    @property
    def remote(self) -> dict[str, Any]:
        return self.data.get("remote") or {}

    @property
    def iterations_dir(self) -> Path:
        # Always beside the case yaml, even when case_dir points elsewhere.
        return self.path.with_suffix("") / "iterations"

    def iteration_dir(self, label: str) -> Path:
        return self.iterations_dir / label

    @property
    def log_path(self) -> Path:
        return self.path.with_name(f"{self.name}.log.yaml")

    def job(self, machine: str) -> dict[str, Any]:
        """Per-machine submission settings (``job.<machine>``)."""
        job = self.data.get("job") or {}
        settings = job.get(machine)
        if not isinstance(settings, dict):
            raise CaseError(f"case {self.name}: no job settings for machine {machine!r}")
        return dict(settings)

    def remote_input_dir(self, machine: str) -> str:
        dirs = self.data.get("remote_input_dir") or {}
        value = dirs.get(machine)
        if not value:
            raise CaseError(f"case {self.name}: remote_input_dir has no entry for {machine!r}")
        return str(value)

    def helper_dir(self, run_root: str) -> str:
        return f"{run_root}/.xgc-flowmatch"

    def resolve_file(self, name: str | None) -> Path:
        """A file named in the case yaml: relative names live in the case directory."""
        if not name:
            raise CaseError(f"case {self.name}: file name missing")
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = self.case_dir / path
        if not path.exists():
            raise CaseError(f"case {self.name}: file not found: {path}")
        return path


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_case_path(name_or_path: str) -> Path:
    path = Path(name_or_path).expanduser()
    if path.suffix in (".yaml", ".yml") and path.exists():
        return path.resolve()
    candidate = CASES_DIR / f"{name_or_path}.yaml"
    if candidate.exists():
        return candidate.resolve()
    raise CaseError(f"case not found: {name_or_path} (looked in {CASES_DIR})")


def load_case(name_or_path: str) -> Case:
    path = resolve_case_path(name_or_path)
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise CaseError(f"{path}: not a mapping")
    data = _merge(DEFAULTS, raw)
    name = str(data.get("name") or path.stem)
    case = Case(name=name, path=path, data=data)
    if not case.case_dir.is_dir():
        raise CaseError(f"case directory missing: {case.case_dir}")
    if not case.template_path.exists():
        raise CaseError(f"input template missing: {case.template_path}")
    for key in ("initial",):
        if not case.flow.get(key):
            raise CaseError(f"case {name}: flow.{key} is required (the starting rotation profile)")
    if not case.target.get("file"):
        raise CaseError(f"case {name}: target.file is required")
    if case.target.get("kind") != "vtor":
        raise CaseError(f"case {name}: target.kind must be 'vtor' (the loop matches the measured toroidal velocity)")
    return case


def remote_python(case: Case, machine_cfg: dict[str, Any]) -> tuple[str, str | None]:
    """(python, PYTHONPATH) for the login-node helper.

    The case may override under ``remote.<machine>`` or ``remote``; otherwise
    the machine's ``analysis`` section in ``machines.local.yaml``, then
    ``xgc.remote_python``. The helper needs adios2, numpy and matplotlib only.
    """
    analysis = machine_cfg.get("analysis") or {}
    remote = case.remote.get(machine_cfg.get("name", ""), case.remote) if case.remote else {}
    python = (
        remote.get("python")
        or analysis.get("python")
        or (machine_cfg.get("xgc") or {}).get("remote_python")
    )
    if not python:
        raise CaseError("no login-node python configured (case remote.python or machine analysis.python)")
    pythonpath = remote.get("pythonpath") or analysis.get("pythonpath")
    return str(python), (str(pythonpath) if pythonpath else None)


# --- iteration log --------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_log(case: Case) -> list[dict[str, Any]]:
    if not case.log_path.exists():
        return []
    with case.log_path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return list(doc.get("entries") or [])


def _write_log(case: Case, entries: list[dict[str, Any]]) -> None:
    doc = {"case": case.name, "entries": entries}
    with case.log_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)


def append_log(case: Case, stage: str, **fields: Any) -> dict[str, Any]:
    entries = read_log(case)
    entry: dict[str, Any] = {"time": _now(), "stage": stage}
    entry.update(fields)
    entries.append(entry)
    _write_log(case, entries)
    return entry


def update_log_entry(case: Case, match: dict[str, Any], **fields: Any) -> bool:
    entries = read_log(case)
    for entry in reversed(entries):
        if all(entry.get(k) == v for k, v in match.items()):
            entry.update(fields)
            entry["updated"] = _now()
            _write_log(case, entries)
            return True
    return False


def iteration_entries(case: Case) -> list[dict[str, Any]]:
    """The staged/submitted iterations in order, newest last."""
    return [e for e in read_log(case) if e.get("stage") == "stage"]


def next_label(case: Case) -> str:
    """``it01``, ``it02``, ... from the existing iteration directories and log."""
    numbers = [0]
    if case.iterations_dir.is_dir():
        for path in case.iterations_dir.iterdir():
            if path.name.startswith("it") and path.name[2:].isdigit():
                numbers.append(int(path.name[2:]))
    for entry in read_log(case):
        label = str(entry.get("label") or "")
        if label.startswith("it") and label[2:].isdigit():
            numbers.append(int(label[2:]))
    return f"it{max(numbers) + 1:02d}"
