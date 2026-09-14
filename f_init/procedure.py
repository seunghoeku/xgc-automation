"""The stages of the f_init procedure (case 0), as deterministic remote actions.

Every stage goes through ``tools/ssh.py`` (absolute rule 2) and returns plain
dictionaries so the CLI and an agent can render them. Nothing here submits or
overwrites without being asked explicitly: ``stage_dt0`` and ``submit`` are
called only after the caller has shown the plan and received confirmation;
``make_finit`` and ``adjust`` refuse to replace an existing file unless told
to. Each stage appends to the case log so a distribution file stays linked
to the run, step, smoothing, and job that produced it.
"""

from __future__ import annotations

import difflib
import json
import posixpath
import re
from pathlib import Path
from typing import Any

from tools import dryrun as dryrun_mod
from tools import machines
from tools import scheduler as sched
from tools import ssh

from . import config as cfg
from . import namelist_edit as nle
from . import sbatch as sb
from .config import Case

REMOTE_DIR = Path(__file__).resolve().parent / "remote"


class ProcedureError(RuntimeError):
    pass


def _machine(case: Case) -> tuple[dict[str, Any], ssh.SSHTarget]:
    machine_cfg = machines.get_machine(case.machine)
    return machine_cfg, ssh.host_for(machine_cfg)


# --- inventory -----------------------------------------------------------

# One round trip lists every subdirectory of the case root with what the
# procedure cares about: an input, the last f0 snapshot, the 2d steps, any
# f_init files, and whether a job there printed "XGC finished".
INVENTORY_SCRIPT = r"""
cd "$1" || { echo "NOROOT"; exit 3; }
for d in */; do
  d="${d%/}"
  [ -d "$d" ] || continue
  inp=0; [ -f "$d/input" ] && inp=1
  f0=$(ls -d "$d"/xgc.f0.[0-9]*.bp 2>/dev/null | sed -n 's/.*xgc\.f0\.\([0-9]*\)\.bp$/\1/p' | sort -n | tail -1)
  d2=$(ls -d "$d"/xgc.2d.[0-9]*.bp 2>/dev/null | sed -n 's/.*xgc\.2d\.\([0-9]*\)\.bp$/\1/p' | sort -n | tr '\n' ' ')
  fin=$(ls -d "$d"/xgc.f_init*.bp 2>/dev/null | sed 's#.*/##' | tr '\n' ' ')
  done_n=$(grep -ls "XGC finished" "$d"/slurm-*.out 2>/dev/null | wc -l | tr -d ' ')
  out_n=$(ls "$d"/slurm-*.out 2>/dev/null | wc -l | tr -d ' ')
  echo "$d|$inp|$f0|$d2|$fin|$done_n|$out_n"
done
"""


def parse_inventory(stdout: str) -> list[dict[str, Any]]:
    dirs: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if line.strip() == "NOROOT":
            raise ProcedureError("case root does not exist on the machine")
        parts = line.rstrip("\n").split("|")
        if len(parts) != 7:
            continue
        name, inp, f0, d2, fin, done_n, out_n = parts
        dirs.append({
            "name": name,
            "has_input": inp == "1",
            "last_f0_step": int(f0) if f0 else None,
            "twod_steps": [int(s) for s in d2.split()],
            "f_init_files": fin.split(),
            "finished_jobs": int(done_n or 0),
            "job_outputs": int(out_n or 0),
        })
    return dirs


def inventory(case: Case) -> dict[str, Any]:
    _cfg, target = _machine(case)
    result = ssh.run(
        target, ["bash", "-c", INVENTORY_SCRIPT, "xgc-finit", case.root], timeout=120
    )
    if result.returncode == 3:
        raise ProcedureError(f"{case.root} does not exist on {case.machine}")
    result.check()
    dirs = parse_inventory(result.stdout)
    maxwell = next((d for d in dirs if d["name"] == case.maxwell_dir), None)
    return {"case": case.name, "root": case.root, "maxwell": maxwell, "dirs": dirs}


# --- remote helper scripts ------------------------------------------------

def _push_helper(case: Case, target: ssh.SSHTarget, script: str) -> str:
    """Copy one login-node script into the case root; idempotent."""
    ssh.mkdir_p(target, case.helper_dir)
    remote_path = f"{case.helper_dir}/{script}"
    ssh.write_file(target, remote_path, (REMOTE_DIR / script).read_text(encoding="utf-8"))
    return remote_path


def _run_json(target: ssh.SSHTarget, argv: list[str], *, timeout: float) -> dict[str, Any]:
    result = ssh.run(target, argv, timeout=timeout)
    payload: dict[str, Any] | None = None
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("FINIT_JSON "):
            payload = json.loads(line[len("FINIT_JSON "):])
            break
    if payload is None:
        tail = "\n".join((result.stdout + "\n" + result.stderr).strip().splitlines()[-25:])
        raise ProcedureError(
            f"remote step produced no result (rc={result.returncode}). Last lines:\n{tail}"
        )
    if "error" in payload:
        paths = ", ".join(payload.get("paths", []))
        if payload["error"] == "exists":
            raise ProcedureError(
                f"refusing to overwrite existing file(s): {paths}. "
                "Pass --overwrite if that is intended."
            )
        raise ProcedureError(f"remote step failed: {payload['error']} {paths}")
    payload["_rc"] = result.returncode
    return payload


# --- stage 2: f0 -> f_init --------------------------------------------------

def make_finit(
    case: Case,
    *,
    sigma_theta: float | None = None,
    v_width: float | None = None,
    step: int | None = None,
    overwrite: bool = False,
    out_dir: str | None = None,
) -> dict[str, Any]:
    machine_cfg, target = _machine(case)
    python, reader = cfg.remote_python(case, machine_cfg)
    sigma = float(case.smooth["sigma_theta"] if sigma_theta is None else sigma_theta)
    width = float(case.smooth["v_width"] if v_width is None else v_width)
    species = case.species

    script = _push_helper(case, target, "make_f_init.py")
    argv = [
        python, script,
        "--xgc-reader", reader,
        "--run-dir", case.maxwell_path,
        "--var-string", str(species["var_string"]),
        "--mass-au", str(species["mass_au"]),
        "--charge-num", str(species.get("charge_num", 1.0)),
        "--sigma-theta", str(sigma),
        "--v-width", str(width),
        "--no-smooth-name", cfg.NO_SMOOTH_FILE,
    ]
    if sigma > 0 or width > 0:
        argv += ["--smooth-name", cfg.smooth_file(sigma, width)]
    if species.get("has_electron"):
        argv.append("--has-electron")
    step = case.f0_step if step is None else step
    if step is not None:
        argv += ["--step", str(step)]
    if overwrite:
        argv.append("--overwrite")
    if out_dir:
        argv += ["--out-dir", out_dir]

    payload = _run_json(target, argv, timeout=3600)
    cfg.append_log(
        case, "make-finit",
        f0_step=payload.get("f0_step"), sigma_theta=sigma, v_width=width,
        outputs=[o["path"] for o in payload.get("outputs", [])],
        f_g_stats=payload.get("f_g_stats"),
    )
    return payload


# --- stage 3: dt=0 directory -----------------------------------------------

def plan_dt0(case: Case, finit_file: str, *, dir_name: str | None = None) -> dict[str, Any]:
    """Everything a dt=0 directory would contain, without writing any of it."""
    finit_name = Path(finit_file).name
    overrides = {
        "sml_dt": str(case.dt0["sml_dt"]),
        "sml_mstep": str(case.dt0["sml_mstep"]),
        # A fresh start even if the source run was restarted several times.
        "sml_restart": ".false.",
    }
    return _plan_from_maxwell(case, finit_name, dir_name or cfg.dt0_dirname(finit_name),
                              overrides, kind="dt0")


def plan_run(case: Case, finit_file: str, *, dir_name: str | None = None) -> dict[str, Any]:
    """A production run from an f_init file: stage A's input with the f_init
    slot switched and the case's ``run.overrides`` applied (cbc3's
    ``finit_sm_*`` convention). ``sml_dt``/``sml_mstep``/``sml_restart`` are
    kept as in stage A so a timed-out job can be restarted."""
    finit_name = Path(finit_file).name
    run_cfg = case.run or {}
    overrides = {str(k): _fortran(v) for k, v in (run_cfg.get("overrides") or {}).items()}
    dir_name = dir_name or cfg.run_dirname(finit_name, str(run_cfg.get("dir_prefix", "finit_")))
    return _plan_from_maxwell(case, finit_name, dir_name, overrides, kind="run")


def _fortran(value: Any) -> str:
    """Render a YAML scalar the way the namelist wants it."""
    if isinstance(value, bool):
        return ".true." if value else ".false."
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in {".true.", ".false."} or stripped.startswith(("'", '"')):
            return stripped
        if re.fullmatch(r"[-+0-9.eEdD]+", stripped):
            return stripped
        return f"'{stripped}'"
    return str(value)


def _plan_from_maxwell(case: Case, finit_name: str, dir_name: str,
                       overrides: dict[str, str], *, kind: str) -> dict[str, Any]:
    _cfg, target = _machine(case)
    target_dir = case.subdir(dir_name)
    script_name = case.job["script"]
    copy_files = list(case.job.get("copy_files", []))

    missing = [
        name for name in copy_files + ["input", script_name, finit_name]
        if not ssh.exists(target, f"{case.maxwell_path}/{name}")
    ]
    if missing:
        raise ProcedureError(
            f"{case.maxwell_path} lacks {missing}; the f_init file must exist "
            "there (run make-finit first) and the job files must be present"
        )

    old_input = ssh.read_file(target, f"{case.maxwell_path}/input")
    slot = int(case.species["slot"])
    overrides = dict(overrides)
    overrides["ptl_input_file_dir"] = f"'../{case.maxwell_dir}/'"
    for name, token in (("ptl_f_init_file", f"'{finit_name}'"),
                        ("ptl_maxwellian_init", ".false.")):
        current = nle.read_value(old_input, name)
        if current is None:
            raise ProcedureError(
                f"{name} is not set in {case.maxwell_path}/input; the procedure "
                "needs the per-species array present to edit one slot"
            )
        overrides[name] = nle.set_slot(current, slot, token)
    new_input, notes = nle.apply(old_input, overrides)
    diff = "".join(difflib.unified_diff(
        old_input.splitlines(keepends=True), new_input.splitlines(keepends=True),
        fromfile=f"{case.maxwell_dir}/input", tofile=f"{dir_name}/input",
    ))

    old_script = ssh.read_file(target, f"{case.maxwell_path}/{script_name}")
    new_script, script_notes = sb.rewrite(old_script, f"{case.name}_{dir_name}")

    return {
        "kind": kind,
        "dir_name": dir_name,
        "target_dir": target_dir,
        "exists": ssh.exists(target, target_dir),
        "finit_file": finit_name,
        "species_slot": slot,
        "copy_files": copy_files,
        "input_text": new_input,
        "input_notes": notes,
        "input_diff": diff,
        "script_name": script_name,
        "script_text": new_script,
        "script_notes": script_notes,
        "resources": sb.resources(new_script),
    }


def stage_dt0(case: Case, plan: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    """Write a planned dt=0 or production-run directory. Call only after the
    plan was approved."""
    _cfg, target = _machine(case)
    target_dir = plan["target_dir"]
    if ssh.exists(target, target_dir) and not overwrite:
        raise ProcedureError(
            f"{target_dir} already exists; choose --dir or pass --overwrite "
            "to replace its input and job script (outputs are left alone)"
        )
    ssh.mkdir_p(target, target_dir)
    for name in plan["copy_files"]:
        ssh.run(target, ["cp", "-p", f"{case.maxwell_path}/{name}", f"{target_dir}/{name}"],
                check=True)
    ssh.write_file(target, f"{target_dir}/input", plan["input_text"])
    ssh.write_file(target, f"{target_dir}/{plan['script_name']}", plan["script_text"])
    entry = cfg.append_log(
        case, "stage-run" if plan.get("kind") == "run" else "stage-dt0",
        dir=plan["dir_name"], finit_file=plan["finit_file"],
        input_notes=plan["input_notes"], script_notes=plan["script_notes"],
        resources=plan["resources"],
    )
    return {"target_dir": target_dir, "written": ["input", plan["script_name"]] + plan["copy_files"],
            "log": entry}


# --- dryrun and submission --------------------------------------------------

def _runscript_name(script_text: str) -> str:
    match = re.search(r"^\s*\./(runscript\S*)", script_text, re.MULTILINE)
    return match.group(1) if match else "runscript.cpu"


def dryrun(case: Case, dir_name: str) -> dict[str, Any]:
    """``xgca --dryrun`` in the directory, using the run script's own modules."""
    _cfg, target = _machine(case)
    directory = case.subdir(dir_name)
    script_text = ssh.read_file(target, f"{directory}/{case.job['script']}")
    runscript = ssh.read_file(target, f"{directory}/{_runscript_name(script_text)}")
    info = sb.parse_runscript(runscript)
    if not info["xgc_bin"]:
        raise ProcedureError(f"no XGC_BIN in {directory}/{_runscript_name(script_text)}")
    steps = ["set -o pipefail", f"cd {sh_quote(directory)}"] + info["modules"] + [
        f"{info['xgc_bin']} --dryrun > dryrun.out 2>&1; rc=$?; cat dryrun.out; exit $rc",
    ]
    # A login shell so that `module` is defined.
    result = ssh.run(target, ["bash", "-lc", "\n".join(steps)], timeout=900)
    report = dryrun_mod.parse_dryrun_output(result.stdout + "\n" + result.stderr, result.returncode)
    return {"dir": dir_name, "exe": info["xgc_bin"], "report": report.to_dict(),
            "blocking": report.blocking, "summary": report.summary()}


def sh_quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


def submit_plan(case: Case, dir_name: str) -> dict[str, Any]:
    """Resources and cost of the job script in ``dir_name``; no side effects."""
    _cfg, target = _machine(case)
    directory = case.subdir(dir_name)
    script_path = f"{directory}/{case.job['script']}"
    if not ssh.exists(target, script_path):
        raise ProcedureError(f"{script_path} does not exist; stage the directory first")
    text = ssh.read_file(target, script_path)
    return {"dir": dir_name, "script": script_path, "resources": sb.resources(text)}


def submit(case: Case, dir_name: str, *, skip_dryrun: bool = False) -> dict[str, Any]:
    """sbatch the directory's job script. Call only after explicit approval."""
    machine_cfg, target = _machine(case)
    plan = submit_plan(case, dir_name)
    gate: dict[str, Any] | None = None
    if not skip_dryrun:
        gate = dryrun(case, dir_name)
        if gate["blocking"]:
            raise ProcedureError(f"dryrun rejected the input; not submitting.\n{gate['summary']}")
    adapter = sched.get_scheduler(machine_cfg)
    result = ssh.run(target, adapter.submit_argv(case.job["script"]),
                     cwd=case.subdir(dir_name), check=True)
    job_id = adapter.parse_job_id(result.stdout)
    entry = cfg.append_log(
        case, "submit", dir=dir_name, job_id=job_id, resources=plan["resources"],
        dryrun=None if gate is None else gate["summary"], state="submitted",
    )
    return {"job_id": job_id, "dir": dir_name, "resources": plan["resources"],
            "dryrun": gate, "log": entry}


FINAL_STATES = {sched.COMPLETED, sched.FAILED, sched.CANCELLED, sched.TIMEOUT}


def job_states(case: Case) -> list[dict[str, Any]]:
    """State of every job this case has submitted, from squeue then sacct."""
    machine_cfg, target = _machine(case)
    adapter = sched.get_scheduler(machine_cfg)
    submitted = [e for e in cfg.read_log(case) if e.get("stage") == "submit" and e.get("job_id")]
    if not submitted:
        return []
    pending = [str(e["job_id"]) for e in submitted if e.get("state") not in FINAL_STATES]
    states: dict[str, tuple[str, str | None]] = {}
    if pending and getattr(adapter, "supports_bulk_status", False):
        queued = ssh.run(target, adapter.bulk_status_argv(pending))
        if queued.ok:
            for job_id, state in adapter.parse_bulk_status(queued.stdout).items():
                states[job_id] = (state, None)
        left = [j for j in pending if j not in states]
        if left:
            acct = ssh.run(target, adapter.bulk_accounting_argv(left))
            if acct.ok:
                states.update(adapter.parse_bulk_accounting(acct.stdout))
    rows = []
    for entry in submitted:
        job_id = str(entry["job_id"])
        state, exit_code = states.get(job_id, (entry.get("state", sched.UNKNOWN), entry.get("exit_code")))
        if state in FINAL_STATES and entry.get("state") != state:
            cfg.update_log_entry(case, {"stage": "submit", "job_id": entry["job_id"]},
                                 state=state, exit_code=exit_code)
        rows.append({"job_id": job_id, "dir": entry.get("dir"), "submitted": entry.get("time"),
                     "state": state, "exit_code": exit_code})
    return rows


# --- stage 4: density comparison and adjustment -----------------------------

def resolve_dt0_finit(case: Case, dt0_dir: str, input_text: str) -> tuple[str, str]:
    """(f_init path, its directory) that a dt=0 input actually points at."""
    slot = int(case.species["slot"])
    finit = nle.array_slot(input_text, "ptl_f_init_file", slot)
    if not finit:
        raise ProcedureError(f"ptl_f_init_file slot {slot} is not set in {dt0_dir}/input")
    raw_dir = nle.unquote(nle.read_value(input_text, "ptl_input_file_dir") or "./")
    base = raw_dir if raw_dir.startswith("/") else posixpath.normpath(
        posixpath.join(case.subdir(dt0_dir), raw_dir))
    return f"{base}/{finit}", base


def adjust(
    case: Case,
    dt0_dir: str,
    *,
    factor: float | None = None,
    check_only: bool = False,
    ref_step: int | None = None,
    cmp_step: int | None = None,
    overwrite: bool = False,
    out_name: str | None = None,
) -> dict[str, Any]:
    machine_cfg, target = _machine(case)
    python, reader = cfg.remote_python(case, machine_cfg)
    directory = case.subdir(dt0_dir)
    input_text = ssh.read_file(target, f"{directory}/input")
    finit_path, finit_dir = resolve_dt0_finit(case, dt0_dir, input_text)
    finit_name = Path(finit_path).name
    out_file = out_name or cfg.adjusted_file(finit_name)
    tag = cfg.finit_tag(finit_name)
    factor = case.adjust_factor if factor is None else float(factor)
    cmp_step = case.dt0.get("compare_step") if cmp_step is None else cmp_step
    stem = f"f_init_{'check' if check_only else 'adjust'}_{tag}"
    json_remote = f"{directory}/{stem}.json"
    png_remote = f"{directory}/{stem}.png"

    script = _push_helper(case, target, "adjust_f_init.py")
    argv = [
        python, script,
        "--xgc-reader", reader,
        "--run-dir", case.maxwell_path,
        "--dt0-dir", directory,
        "--finit", finit_path,
        "--factor", str(factor),
        "--json-out", json_remote,
        "--png-out", png_remote,
    ]
    argv += ["--region-max", str(int(case.extra.get("adjust_region_max", 1)))]
    if check_only:
        argv.append("--check-only")
    else:
        argv += ["--out", f"{finit_dir}/{out_file}"]
    ref_step = case.f0_step if ref_step is None else ref_step
    if ref_step is not None:
        argv += ["--ref-step", str(ref_step)]
    if cmp_step is not None:
        argv += ["--cmp-step", str(cmp_step)]
    if overwrite:
        argv.append("--overwrite")

    payload = _run_json(target, argv, timeout=1800)

    case.artifacts_dir.mkdir(parents=True, exist_ok=True)
    local: dict[str, str] = {}
    for key, remote in (("json", json_remote), ("png", png_remote)):
        if payload.get(key):
            local_path = case.artifacts_dir / f"{dt0_dir}__{stem}.{key}"
            try:
                ssh.pull(target, remote, str(local_path))
                local[key] = str(local_path)
            except ssh.SSHError as exc:
                local[f"{key}_error"] = str(exc)
    payload["local"] = local
    cfg.append_log(
        case, "check" if check_only else "adjust",
        dt0_dir=dt0_dir, finit=finit_path, ref_step=payload.get("ref_step"),
        cmp_step=payload.get("cmp_step"), factor=factor,
        metrics=payload.get("metrics"), output=payload.get("output"), local=local,
    )
    return payload


# --- initial-transient comparison (paper figures) ---------------------------

def compare(
    case: Case,
    dirs: list[str] | None = None,
    *,
    labels: list[str] | None = None,
    psi: float = 0.45,
    f0_step: int | None = None,
    particles: bool = True,
    end_window_ms: float = 0.04,
    shift_finit: bool = False,
    paper: bool = False,
    twod_runs: list[str] | None = None,
    twod_scale: str = "shared",
) -> dict[str, Any]:
    """Run ``remote/transient_compare.py`` over the Maxwellian run and the
    production runs (default: every ``finit_*`` directory in the inventory),
    then pull the JSON and PNGs into the case's artifacts directory."""
    machine_cfg, target = _machine(case)
    python, reader = cfg.remote_python(case, machine_cfg)
    prefix = str((case.run or {}).get("dir_prefix", "finit_"))
    if not dirs:
        inv = inventory(case)
        dirs = sorted(
            (d["name"] for d in inv["dirs"] if d["name"].startswith(prefix) and d["has_input"]),
            key=_smooth_sort_key,
        )
    if not dirs:
        raise ProcedureError(f"no {prefix}* directories under {case.root}; run stage-run first")
    if labels and len(labels) != len(dirs):
        raise ProcedureError("--label must be given once per directory")
    labels = labels or [_default_label(d, prefix) for d in dirs]

    f0_step = case.f0_step if f0_step is None else f0_step
    out_dir = f"{case.root}/.xgc-finit/compare"
    ssh.mkdir_p(target, out_dir)
    stem = f"transient_psi{psi:g}" + ("_shift" if shift_finit else "") + ("_paper" if paper else "")
    remote_prefix = f"{out_dir}/{stem}"
    script = _push_helper(case, target, "transient_compare.py")
    argv = [python, script, "--xgc-reader", reader, "--run-dir", case.maxwell_path,
            "--psi", str(psi), "--end-window", str(end_window_ms), "--out-prefix", remote_prefix]
    for d, lbl in zip(dirs, labels):
        color, style = _line_appearance(d, prefix)
        # '=' form: a style such as '-.' would otherwise be read as an option.
        argv += ["--finit-dir", case.subdir(d), "--label", lbl, f"--color={color}", f"--style={style}"]
    if f0_step is not None:
        argv += ["--f0-step", str(f0_step)]
    if not particles:
        argv.append("--no-particles")
    if shift_finit:
        argv.append("--shift-finit")
    if paper:
        argv.append("--paper")
    if twod_runs:
        argv += ["--twod-runs", *twod_runs]
    if twod_scale != "shared":
        argv += ["--twod-scale", twod_scale]

    payload = _run_json(target, argv, timeout=3600)

    case.artifacts_dir.mkdir(parents=True, exist_ok=True)
    local: dict[str, str] = {}
    pulls = [("json", payload["json"])] + [(f"png_{k}", v) for k, v in payload.get("png", {}).items()]
    for key, remote in pulls:
        local_path = case.artifacts_dir / Path(remote).name
        try:
            ssh.pull(target, remote, str(local_path))
            local[key] = str(local_path)
        except ssh.SSHError as exc:
            local[f"{key}_error"] = str(exc)
    payload["local"] = local
    cfg.append_log(
        case, "compare", dirs=dirs, labels=labels, psi=payload.get("psi_at"),
        f0_step=payload.get("f0_step"), shift_finit=shift_finit, paper=paper,
        summary=payload.get("summary"), local=local,
    )
    return payload


def _smooth_sort_key(name: str) -> tuple[float, float]:
    nums = re.findall(r"[0-9.]+", name)
    try:
        return (float(nums[-2]), float(nums[-1]))
    except (IndexError, ValueError):
        return (float("inf"), float("inf"))


# Fixed colours so a curve keeps its identity when runs are added to the scan:
# Maxwellian is always C0 (blue); the smoothing settings below keep the
# matplotlib cycle they were first drawn with (0.5 rad = C3, red).
_APPEARANCE: dict[tuple[float, float], tuple[str, str]] = {
    (0.0, 0.0): ("black", "-."),
    (0.0, 0.5): ("C1", "-."),
    (0.3, 0.5): ("C2", "--"),
    (0.5, 0.5): ("C3", "-"),
    (0.8, 0.5): ("C4", ":"),
    (1.0, 0.5): ("C5", "(0, (3, 1, 1, 1, 1, 1))"),
}
_FALLBACK_COLOURS = ["C6", "C7", "C8", "C9"]


def _line_appearance(dir_name: str, prefix: str) -> tuple[str, str]:
    key = _smooth_sort_key(dir_name)
    if key in _APPEARANCE:
        return _APPEARANCE[key]
    used = {v[0] for v in _APPEARANCE.values()}
    spare = [c for c in _FALLBACK_COLOURS if c not in used]
    idx = abs(hash(key)) % len(spare)
    return spare[idx], "--"


def _default_label(dir_name: str, prefix: str) -> str:
    """``finit_sm_0.5_0.5 -> 'pol. smooth 0.5 rad'``; ``finit_sm_0_0.5 -> 'v smooth, no pol. smooth'``."""
    tag = dir_name[len(prefix):] if dir_name.startswith(prefix) else dir_name
    match = re.match(r"^sm_([0-9.]+)_([0-9.]+)$", tag)
    if not match:
        return dir_name
    sigma, width = float(match.group(1)), float(match.group(2))
    if sigma == 0 and width == 0:
        return "No smooth"
    if sigma == 0:
        return "v smooth, no pol. smooth"
    return f"pol. smooth {cfg.fmt_num(sigma)} rad"


# --- stage A continuation ---------------------------------------------------

def plan_extend_maxwell(case: Case) -> dict[str, Any]:
    """Resubmit the stage A job so XGC continues from its last checkpoint.

    XGC with ``sml_restart=.true.`` and a populated ``restart_dir`` resumes from
    the latest ``xgc.restart.<step>.bp`` and runs another ``sml_mstep`` steps
    (cbc3's maxwell reached step 4000 this way with sml_mstep=2000 unchanged).
    The input is therefore left alone; only the sbatch script is rewritten
    (job name, stale dependency dropped). Nothing is written without confirm."""
    _cfg, target = _machine(case)
    directory = case.maxwell_path
    input_text = ssh.read_file(target, f"{directory}/input")
    restart = nle.read_value(input_text, "sml_restart")
    if restart is None or restart.strip().lower() != ".true.":
        raise ProcedureError(f"{directory}/input has sml_restart={restart!r}; the continuation needs .true.")
    listing = ssh.run(target, ["ls", f"{directory}/restart_dir"], timeout=60)
    steps = sorted(int(m.group(1)) for m in re.finditer(r"xgc\.restart\.(\d+)\.bp", listing.stdout))
    if not steps:
        raise ProcedureError(f"{directory}/restart_dir holds no xgc.restart.*.bp; nothing to continue from")
    inv = inventory(case)
    maxwell = inv["maxwell"] or {}
    mstep = nle.read_value(input_text, "sml_mstep")
    script_name = case.job["script"]
    old_script = ssh.read_file(target, f"{directory}/{script_name}")
    new_script, notes = sb.rewrite(old_script, f"{case.name}_{case.maxwell_dir}_from{steps[-1]}")
    return {
        "dir_name": case.maxwell_dir, "target_dir": directory,
        "restart_step": steps[-1], "last_2d_step": (maxwell.get("twod_steps") or [None])[-1],
        "sml_mstep": mstep, "expected_end_step": steps[-1] + int(float(mstep.replace("D", "E"))) if mstep else None,
        "script_name": script_name, "script_text": new_script, "script_notes": notes,
        "resources": sb.resources(new_script),
    }


def extend_maxwell(case: Case, plan: dict[str, Any]) -> dict[str, Any]:
    """Write the rewritten sbatch script and submit it (dryrun gate inside submit)."""
    _cfg, target = _machine(case)
    ssh.write_file(target, f"{plan['target_dir']}/{plan['script_name']}", plan["script_text"])
    cfg.append_log(case, "extend-maxwell", dir=plan["dir_name"], restart_step=plan["restart_step"],
                   expected_end_step=plan["expected_end_step"], script_notes=plan["script_notes"])
    return submit(case, plan["dir_name"])


# --- paper Fig. 1 and the relaxed E_r profile --------------------------------

def distribution_figure(case: Case, *, no_smooth: str | None = None, smooth: str | None = None,
                        smooth_label: str | None = None, psi: float = 0.45, node: int | None = None,
                        clip_percent: float | None = None, paper: bool = False,
                        normalize: str = "local") -> dict[str, Any]:
    """Relative deviation (f - f_M)/f_M of the unsmoothed and smoothed f_init at one node."""
    machine_cfg, target = _machine(case)
    python, reader = cfg.remote_python(case, machine_cfg)
    sigma, width = float(case.smooth["sigma_theta"]), float(case.smooth["v_width"])
    no_smooth = no_smooth or cfg.NO_SMOOTH_FILE
    smooth = smooth or cfg.smooth_file(sigma, width)
    smooth_label = smooth_label or f"$\\Delta\\theta={cfg.fmt_num(sigma)}$ rad, ${cfg.fmt_num(width)}\\Delta v$"
    out_dir = f"{case.root}/.xgc-finit/compare"
    ssh.mkdir_p(target, out_dir)
    stem = "distribution" + ("_paper" if paper else "")
    script = _push_helper(case, target, "distribution_figure.py")
    argv = [python, script, "--xgc-reader", reader, "--run-dir", case.maxwell_path,
            "--no-smooth", f"{case.maxwell_path}/{no_smooth}", "--smooth", f"{case.maxwell_path}/{smooth}",
            "--smooth-label", smooth_label, "--psi", str(psi), "--out-prefix", f"{out_dir}/{stem}"]
    if node is not None:
        argv += ["--node", str(node)]
    if clip_percent is not None:
        argv += ["--clip-percent", str(clip_percent)]
    if paper:
        argv.append("--paper")
    argv += ["--normalize", normalize]
    payload = _run_json(target, argv, timeout=1800)
    payload["local"] = _pull_outputs(case, target, payload)
    cfg.append_log(case, "distribution-figure", files=[no_smooth, smooth], where=payload.get("where"),
                   normalize=normalize,
                   clip_percent=payload.get("clip_percent"), panels=payload.get("panels"),
                   local=payload["local"])
    return payload


def er_profile(case: Case, *, f0_step: int | None = None, paper: bool = False,
               psi_max: float = 0.9) -> dict[str, Any]:
    """<E_r>(psi_N) of the stage A run at the f_init snapshot and at the end."""
    machine_cfg, target = _machine(case)
    python, reader = cfg.remote_python(case, machine_cfg)
    f0_step = case.f0_step if f0_step is None else f0_step
    if f0_step is None:
        inv = inventory(case)
        f0_step = (inv["maxwell"] or {}).get("last_f0_step")
    if f0_step is None:
        raise ProcedureError("no f0 step known; set f0_step in the case file or pass --f0-step")
    out_dir = f"{case.root}/.xgc-finit/compare"
    ssh.mkdir_p(target, out_dir)
    stem = "er_profile" + ("_paper" if paper else "")
    script = _push_helper(case, target, "er_profile.py")
    argv = [python, script, "--xgc-reader", reader, "--run-dir", case.maxwell_path,
            "--f0-step", str(f0_step), "--psi-max", str(psi_max), "--out-prefix", f"{out_dir}/{stem}"]
    if paper:
        argv.append("--paper")
    payload = _run_json(target, argv, timeout=1800)
    payload["local"] = _pull_outputs(case, target, payload)
    cfg.append_log(case, "er-profile", psi_max=psi_max, steps=payload.get("steps"), time_ms=payload.get("time_ms"),
                   er_range_kv_per_m=payload.get("er_range_kv_per_m"), local=payload["local"])
    return payload


def _pull_outputs(case: Case, target: ssh.SSHTarget, payload: dict[str, Any]) -> dict[str, str]:
    case.artifacts_dir.mkdir(parents=True, exist_ok=True)
    local: dict[str, str] = {}
    pulls = [("json", payload["json"])] + [(f"png_{k}", v) for k, v in payload.get("png", {}).items()]
    for key, remote in pulls:
        local_path = case.artifacts_dir / Path(remote).name
        try:
            ssh.pull(target, remote, str(local_path))
            local[key] = str(local_path)
        except ssh.SSHError as exc:
            local[f"{key}_error"] = str(exc)
    return local
