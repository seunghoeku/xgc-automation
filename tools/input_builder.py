"""Build XGC input files from a validated baseline plus overrides.

The workflow is deliberately case-based rather than generative. Rather than
having the LLM write a namelist from scratch -- 708 parameters, most of them
coupled -- we start from a case that is known to pass `xgc --dryrun` and apply
a small set of changes to it.

Every change is checked against XGC's own parameter metadata before it is
written, so a misspelled name or an invalid enum value is caught here rather
than surfacing as a confusing runtime failure.

Like submission, this is two-phase (AGENTS.md, absolute rule 1):

    plan = plan_input(...)      # renders the new file, produces a diff
    # human reviews the diff
    write_input(..., confirm=True)

Nothing is written remotely without confirm=True.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tools import dryrun as dryrun_mod
from tools import machines as m
from tools import namelist as nlm
from tools import ssh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = PROJECT_ROOT / "kb" / "cases"


class InputBuildError(RuntimeError):
    """The requested input could not be built."""


def list_cases() -> list[dict[str, Any]]:
    """Known baseline cases from kb/cases/."""
    cases: list[dict[str, Any]] = []
    if not CASES_DIR.exists():
        return cases
    for path in sorted(CASES_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cases.append(
            {
                "name": data.get("name", path.stem),
                "description": (data.get("description") or "").strip(),
                "machine": data.get("machine"),
                "case_dir": data.get("case_dir"),
                "validated": data.get("validated", {}),
                "physics": data.get("physics", {}),
            }
        )
    return cases


def load_case(name: str) -> dict[str, Any]:
    """Load one baseline case definition."""
    path = CASES_DIR / f"{name}.yaml"
    if not path.exists():
        known = [c["name"] for c in list_cases()]
        raise InputBuildError(f"unknown case {name!r}; known cases: {known}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class InputPlan:
    """A proposed input file, ready for human review."""

    case: str
    machine: str
    source_path: str
    target_path: str
    content: str
    diff: str
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "machine": self.machine,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "changes": self.changes,
            "warnings": self.warnings,
            "diff": self.diff,
            "content": self.content,
        }


def describe_parameter(machine: str, name: str) -> dict[str, Any]:
    """What XGC's source says about a parameter.

    Answers from the generated metadata rather than from model recall, so the
    type, default, and valid options are whatever the current source says.
    """
    metadata = nlm.load_metadata(machine)
    info = nlm.find_parameter(metadata, name)
    if info is None:
        return {
            "found": False,
            "name": name,
            "did_you_mean": nlm.suggest_names(metadata, name),
        }
    out = info.to_dict()
    out["found"] = True
    return out


def _read_remote_text(machine: str, path: str) -> str:
    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)
    if not ssh.exists(host, path):
        raise InputBuildError(f"{path} not found on {machine}")
    return ssh.read_file(host, path)


def plan_input(
    case: str,
    overrides: dict[str, Any],
    *,
    target_path: str | None = None,
    machine: str | None = None,
) -> InputPlan:
    """Apply overrides to a baseline case and produce a reviewable diff.

    Writes nothing. Validates every override against XGC's metadata first: an
    unknown parameter name or an invalid value raises rather than producing a
    file that fails later.
    """
    case_def = load_case(case)
    machine = machine or case_def.get("machine")
    if not machine:
        raise InputBuildError(f"case {case!r} has no machine and none was given")

    case_dir = case_def.get("case_dir")
    if not case_dir:
        raise InputBuildError(f"case {case!r} has no case_dir")
    source_path = f"{case_dir}/input"

    metadata = nlm.load_metadata(machine)
    original = _read_remote_text(machine, source_path)

    # Validate everything before touching the text, so a bad override cannot
    # leave a half-applied result.
    problems: list[str] = []
    rendered: dict[str, str] = {}
    for name, value in overrides.items():
        info = nlm.find_parameter(metadata, name)
        if info is None:
            suggestions = nlm.suggest_names(metadata, name)
            hint = f"; did you mean {suggestions}?" if suggestions else ""
            problems.append(f"unknown parameter {name!r}{hint}")
            continue
        problems.extend(nlm.validate_value(value, info))
        try:
            rendered[name] = nlm.format_value(value, info)
        except nlm.NamelistError as exc:
            problems.append(str(exc))

    if problems:
        raise InputBuildError(
            "invalid overrides:\n  " + "\n  ".join(problems)
        )

    updated, changes = nlm.apply_overrides(original, rendered, metadata)

    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"{case}/input (baseline)",
            tofile=target_path or f"{case}/input (proposed)",
            n=2,
        )
    )

    warnings: list[str] = []
    if not changes:
        warnings.append("no changes applied; the values already match")
    if target_path is None:
        warnings.append("no target_path given; write_input will refuse to overwrite the baseline")

    return InputPlan(
        case=case,
        machine=machine,
        source_path=source_path,
        target_path=target_path or "",
        content=updated,
        diff=diff,
        changes=changes,
        warnings=warnings,
    )


def write_input(
    plan: InputPlan,
    *,
    confirm: bool = False,
    validate: bool = True,
    n_ranks: int | None = None,
) -> dict[str, Any]:
    """Write a planned input to the remote, after confirmation.

    Refuses to overwrite the baseline case directory: baselines are references
    and should stay reproducible. Write to a new directory instead.

    By default the written case is validated with `xgc --dryrun`, and a
    rejected input is reported (the file is left in place so it can be
    inspected and fixed).
    """
    if not plan.target_path:
        raise InputBuildError(
            "plan has no target_path; pass one to plan_input to choose where to write"
        )

    case_def = load_case(plan.case)
    baseline_dir = (case_def.get("case_dir") or "").rstrip("/")
    target_dir = str(Path(plan.target_path).parent).rstrip("/")
    if baseline_dir and target_dir == baseline_dir:
        raise InputBuildError(
            f"refusing to overwrite the baseline case at {baseline_dir}. "
            f"Write to a separate directory so the baseline stays reproducible."
        )

    if not confirm:
        return {
            "written": False,
            "reason": "confirmation required; review the diff and resubmit with confirm=True",
            "plan": plan.to_dict(),
        }

    cfg = m.get_machine(plan.machine)
    host = ssh.host_for(cfg)

    ssh.mkdir_p(host, target_dir)
    ssh.write_file(host, plan.target_path, plan.content)

    result: dict[str, Any] = {
        "written": True,
        "machine": plan.machine,
        "target_path": plan.target_path,
        "changes": plan.changes,
    }

    if validate:
        report = dryrun_mod.run_dryrun(plan.machine, target_dir, n_ranks=n_ranks)
        result["dryrun"] = report.to_dict()
        result["valid"] = report.ok
        if not report.ok:
            result["reason"] = f"written but rejected: {report.summary()}"

    return result


def stage_case(
    case: str,
    target_dir: str,
    *,
    confirm: bool = False,
    machine: str | None = None,
) -> dict[str, Any]:
    """Copy a baseline case's input files into a new working directory.

    XGC needs the mesh, equilibrium, and profile files alongside `input`. This
    copies them remotely rather than through the local machine -- the mesh
    files alone are several MB and must not be round-tripped (AGENTS.md, data
    principles).
    """
    case_def = load_case(case)
    machine = machine or case_def.get("machine")
    case_dir = (case_def.get("case_dir") or "").rstrip("/")
    if not case_dir:
        raise InputBuildError(f"case {case!r} has no case_dir")

    target_dir = target_dir.rstrip("/")
    if target_dir == case_dir:
        raise InputBuildError("target_dir is the baseline itself")

    if not confirm:
        return {
            "staged": False,
            "reason": "confirmation required; resubmit with confirm=True",
            "source": case_dir,
            "target": target_dir,
        }

    cfg = m.get_machine(machine)
    host = ssh.host_for(cfg)

    if ssh.exists(host, target_dir):
        listing = ssh.run(host, ["ls", "-A", target_dir])
        if listing.ok and listing.stdout.strip():
            raise InputBuildError(
                f"{target_dir} already exists and is not empty; "
                f"refusing to overwrite. Choose another directory."
            )

    ssh.mkdir_p(host, target_dir)
    # -a preserves the input_dir/ tree; trailing /. copies contents not the dir.
    ssh.run(host, ["cp", "-a", f"{case_dir}/.", target_dir], check=True, timeout=600.0)

    listing = ssh.run(host, ["ls", "-1", target_dir])
    return {
        "staged": True,
        "machine": machine,
        "source": case_dir,
        "target": target_dir,
        "files": listing.stdout.split() if listing.ok else [],
    }
