"""The flow-matching iteration: analyze -> adjust -> plan -> stage -> submit.

One iteration of the search is one catalogued simulation. The loop is

    omega_(k+1)(psi) = omega_k(psi) + relax * smooth( omega_target - omega_sim,k )

where ``omega_sim,k`` is the carbon parallel flow of run k at the outboard
midplane, converted to a rotation frequency with the same geometry factor
XGC uses for its input (``eq_flow_type=2``: ``u_par = omega R B_phi/B``), and
``omega_target`` is the measured carbon toroidal velocity divided by the
midplane major radius (or a rotation-frequency file directly). This is the
fixed-point update of the notebook
``adjust_flow_5th_from_turb_sim.ipynb``, made repeatable.

Placement (docs/data-analysis.md): the midplane extraction reads a few
hundred f3d steps and is file-access dominant, so it runs on the login node
with a bounded timeout. The profile arithmetic is local. Submissions go
through ``tools.hpc_runner.submit`` so every iteration gets a reserved
``simulation_id``, provenance, input backup and catalog record like any
other run (AGENTS.md rules 1, 2, 4, 5).
"""

from __future__ import annotations

import difflib
import json
import shlex
from pathlib import Path
from typing import Any

from f_init import namelist_edit as nle
from tools import machines
from tools import simulation_catalog as sc
from tools import simulation_registry as sr
from tools import ssh
from tools import paths

from . import backends as be
from . import campaign as camp
from . import config as cfg

REMOTE_DIR = Path(__file__).resolve().parent / "remote"
HELPER = "midplane_flow.py"
_MARK = "FLOWMATCH_JSON "


class ProcedureError(RuntimeError):
    """A stage cannot proceed."""


# --- machine plumbing ------------------------------------------------------


def _machine(case: cfg.Case, machine: str | None) -> tuple[str, dict[str, Any], ssh.SSHTarget]:
    name = machine or case.machine
    machine_cfg = machines.get_machine(name)
    machine_cfg = dict(machine_cfg)
    machine_cfg.setdefault("name", name)
    return name, machine_cfg, ssh.host_for(machine_cfg)


def _run_root(machine_cfg: dict[str, Any]) -> str:
    paths = machine_cfg.get("paths") or {}
    root = paths.get("run_root") or paths.get("scratch")
    if not root:
        raise ProcedureError("machine has no paths.run_root")
    return str(root)


def _push_helper(case: cfg.Case, target: ssh.SSHTarget, run_root: str) -> str:
    helper_dir = case.helper_dir(run_root)
    ssh.mkdir_p(target, helper_dir)
    remote_path = f"{helper_dir}/{HELPER}"
    ssh.write_file(target, remote_path, (REMOTE_DIR / HELPER).read_text(encoding="utf-8"))
    return remote_path


def _run_json(target: ssh.SSHTarget, argv: list[str], *, timeout: float) -> dict[str, Any]:
    result = ssh.run(target, argv, timeout=timeout)
    lines = (result.stdout or "").splitlines()
    for line in reversed(lines):
        if line.startswith(_MARK):
            payload = json.loads(line[len(_MARK):])
            payload["_rc"] = result.returncode
            if payload.get("error"):
                raise ProcedureError(f"remote helper failed: {payload['error']} {payload}")
            return payload
    tail = "\n".join(lines[-25:] + (result.stderr or "").splitlines()[-25:])
    raise ProcedureError(f"remote helper printed no result (rc={result.returncode}):\n{tail}")


# --- profiles --------------------------------------------------------------


def load_prf(path: Path) -> tuple[Any, Any]:
    import numpy as np

    with path.open(encoding="utf-8") as fh:
        n = int(fh.readline().split()[0])
        psi = np.zeros(n)
        var = np.zeros(n)
        for i in range(n):
            a, b = fh.readline().split()[:2]
            psi[i], var[i] = float(a), float(b)
    return psi, var


def save_prf(path: Path, psi, var) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"{len(psi)}\n")
        for x, y in zip(psi, var):
            fh.write(f"{x:19.13e}  {y:19.13e}\n")
        fh.write("-1\n")


def _savgol(values, spec: dict[str, Any] | None):
    if not spec:
        return values
    window = int(spec.get("window", 0))
    order = int(spec.get("order", 3))
    if window < 3 or window > len(values):
        return values
    if window % 2 == 0:
        window -= 1
    if order >= window:
        order = window - 1
    from scipy.signal import savgol_filter

    return savgol_filter(values, window, order)


# --- analyze ---------------------------------------------------------------


def analyze(
    case: cfg.Case,
    run_dir: str,
    *,
    label: str,
    machine: str | None = None,
    last: int | None = None,
    step_min: int | None = None,
    step_max: int | None = None,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Extract the midplane carbon flow of one run and pull the summary.

    ``run_dir`` is the remote run directory. Results land in
    ``iterations/<label>/`` and the log gets an ``analyze`` entry.
    """
    name, machine_cfg, target = _machine(case, machine)
    run_root = _run_root(machine_cfg)
    python, pythonpath = cfg.remote_python(case, machine_cfg)
    script = _push_helper(case, target, run_root)
    out_dir = f"{run_dir.rstrip('/')}/flow_match"
    ssh.mkdir_p(target, out_dir)
    stem = f"{out_dir}/midplane_{label}"
    analysis = case.analysis
    argv: list[str] = []
    if pythonpath:
        argv += ["env", f"PYTHONPATH={pythonpath}"]
    argv += [
        python,
        script,
        "--run-dir",
        run_dir,
        "--var",
        str(case.species.get("f3d_var", "i2u_para")),
        "--last",
        str(last if last is not None else analysis.get("last", 500)),
        "--nr",
        str(analysis.get("nr", 400)),
        "--probes",
        ",".join(str(p) for p in analysis.get("probes", [])),
        "--json-out",
        f"{stem}.json",
        "--prf-out",
        f"{stem}.prf",
        "--png-out",
        f"{stem}.png",
    ]
    if step_min is not None:
        argv += ["--step-min", str(step_min)]
    if step_max is not None:
        argv += ["--step-max", str(step_max)]
    payload = _run_json(target, argv, timeout=timeout)

    local_dir = case.iteration_dir(label)
    local_dir.mkdir(parents=True, exist_ok=True)
    local: dict[str, str] = {}
    for key, remote_path in (("json", f"{stem}.json"), ("prf", f"{stem}.prf"), ("png", f"{stem}.png")):
        dest = paths.product_target(local_dir / Path(remote_path).name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        ssh.pull(target, remote_path, str(dest))
        local[key] = str(dest)

    archive = None
    if camp.enabled(case):
        archive = camp.update_archive(
            case,
            label,
            machine=name,
            machine_cfg=machine_cfg,
            target=target,
            run_dir=run_dir,
            analysis_stem=stem,
            window=payload.get("window"),
            diag_prefix=str(payload.get("diag_prefix", "f2d")),
            input_prf_remote=_input_prf_remote(case, label),
        )

    tol = float(analysis.get("saturation_tol", 0.05))
    worst = max(
        (abs(s["relative_change"]) for s in payload.get("saturation", []) if s.get("relative_change") is not None),
        default=None,
    )
    saturated = None if worst is None else bool(worst <= tol)
    entry = cfg.append_log(
        case,
        "analyze",
        label=label,
        machine=name,
        run_dir=run_dir,
        window=payload.get("window"),
        saturation=payload.get("saturation"),
        saturated=saturated,
        local=local,
        archive=archive,
    )
    return {"payload": payload, "local": local, "saturated": saturated, "worst_relative_change": worst, "archive": archive, "log": entry}


def _input_prf_remote(case: cfg.Case, label: str) -> str | None:
    for entry in reversed(cfg.read_log(case)):
        if entry.get("stage") == "stage" and entry.get("label") == label:
            return entry.get("prf_remote")
    return None


# --- adjust ----------------------------------------------------------------


def target_vtor(case: cfg.Case):
    """(psi, v_tor_target) in m/s on the target file's own grid, within target.psi_max."""
    import numpy as np

    target = case.target
    psi_t, value = load_prf(case.resolve_file(target["file"]))
    scale = float(target.get("scale", 1.0))
    if target["kind"] != "vtor":
        raise ProcedureError("target.kind must be 'vtor': the loop matches the measured toroidal velocity")
    v_t = value * scale
    psi_max = target.get("psi_max")
    if psi_max is not None:
        keep = psi_t <= float(psi_max) + 1e-12
        psi_t, v_t = psi_t[keep], v_t[keep]
    return psi_t, v_t


def adjust(
    case: cfg.Case,
    label: str,
    *,
    sim_json: Path | None = None,
    current_prf: Path | None = None,
    relax: float | None = None,
    out_name: str | None = None,
) -> dict[str, Any]:
    """Build the next rotation profile from the analysis of iteration ``label``.

    The comparison is made in toroidal velocity at the outboard midplane:

        v_sim    = u_para * B_phi/B                 (carbon parallel flow projected)
        delta_v  = v_target - v_sim                 (CER carbon v_tor)
        v_input  = omega * R * (B_phi/B)^2          (what the input omega implies,
                                                     eq_flow_type=2: u_par = omega R B_phi/B)
        omega_next = omega + relax * delta_v / (R (B_phi/B)^2)

    ``current_prf`` is the profile the analysed run used (default: the
    ``input_prf`` recorded for the label, else ``flow.initial``). The new
    profile is written to ``iterations/<label>/`` and logged; nothing is
    pushed to any machine here.
    """
    import numpy as np

    local_dir = case.iteration_dir(label)
    source: str
    if sim_json is None and camp.enabled(case) and camp.local_archive(case).exists():
        # The archive is the ledger: read the analysis from the study .aca.
        sim = camp.read_midplane(case, label)
        source = f"archive:{camp.local_archive(case)}#{label}/midplane.json"
    else:
        sim_json = sim_json or (local_dir / f"midplane_{label}.json")
        if not Path(sim_json).exists():
            raise ProcedureError(f"analysis result missing: {sim_json} (run analyze first)")
        with Path(sim_json).open(encoding="utf-8") as fh:
            sim = json.load(fh)
        source = str(sim_json)
    mid = sim["midplane"]
    psi_sim = np.asarray(mid["psi_n"])
    r_sim = np.asarray(mid["r"])
    bratio_sim = np.asarray(mid["bphi_over_b"])
    v_sim = np.asarray(mid["vtor"]) if "vtor" in mid else np.asarray(mid["u_para"]) * bratio_sim

    if current_prf is None:
        current_prf = _input_prf_for(case, label)
    psi_in, omega_in = load_prf(Path(current_prf))

    psi_t, v_t = target_vtor(case)
    adj = case.adjust
    psi_lo = max(float(adj.get("psi_min", 0.0)), float(psi_sim.min()), float(psi_t.min()))
    psi_hi = min(float(psi_sim.max()), float(psi_t.max()))  # trusted range for the metrics
    valid = (psi_in >= psi_lo) & (psi_in <= psi_hi)
    if valid.sum() < 5:
        raise ProcedureError(f"no overlap between input grid, simulation ({psi_sim.min():.3f}-{psi_sim.max():.3f}) and target ({psi_t.min():.3f}-{psi_t.max():.3f})")

    # Everything on the input file's psi grid.
    v_sim_p = np.interp(psi_in, psi_sim, v_sim)
    v_t_p = np.interp(psi_in, psi_t, v_t)
    sol = adj.get("sol") or {}
    mode = str(sol.get("mode", "follow"))
    if mode == "follow":
        v_t_p, v_ext_info = _extend_target(psi_in, psi_t, v_t, sol)
    else:
        v_ext_info = None
    r_p = np.interp(psi_in, psi_sim, r_sim)
    bratio_p = np.interp(psi_in, psi_sim, bratio_sim)
    geom = r_p * bratio_p**2  # v_tor per unit omega
    delta_v = v_t_p - v_sim_p

    # Inside psi_min hold the inner value. Beyond psi_start (the separatrix by
    # default) the measurement ends: `follow` keeps correcting toward the
    # smoothly extrapolated target, `hold` carries the separatrix correction,
    # `constant` / `zero` impose a value.
    psi_start = min(float(sol.get("psi_start", psi_hi)), psi_hi)
    idx_valid = np.nonzero(valid)[0]
    first, last = idx_valid[0], idx_valid[-1]
    delta_v[:first] = delta_v[first]
    outer = psi_in > psi_start
    if outer.any():
        if mode == "follow":
            sim_ok = psi_in <= float(psi_sim.max())
            # Where the simulation ends too, carry the last available correction.
            if (~sim_ok).any():
                delta_v[~sim_ok] = delta_v[sim_ok][-1]
            taper = float(sol.get("taper", 0.0) or 0.0)
            if taper > 0:
                # Optional Gaussian taper of the SOL correction, width in psi_n.
                delta_v[outer] *= np.exp(-(((psi_in[outer] - psi_start) / taper) ** 2))
        elif mode == "hold":
            delta_v[outer] = np.interp(psi_start, psi_in, delta_v)
        elif mode == "constant":
            delta_v[outer] = float(sol.get("value", 0.0))
        elif mode == "zero":
            delta_v[outer] = 0.0
        else:
            raise ProcedureError(f"adjust.sol.mode {mode!r} unknown (follow, hold, constant, zero)")
    elif last + 1 < delta_v.size:
        delta_v[last + 1 :] = delta_v[last]

    delta_v_s = _savgol(delta_v, adj.get("smooth_delta"))
    factor = float(relax if relax is not None else adj.get("relax", 1.0))
    delta_omega = delta_v_s / np.where(np.abs(geom) > 1e-12, geom, np.sign(geom) * 1e-12 + 1e-12)
    omega_new = _savgol(omega_in + factor * delta_omega, adj.get("smooth_new"))

    rms_delta = float(np.sqrt(np.mean(delta_v[valid] ** 2)))
    rms_target = float(np.sqrt(np.mean(v_t_p[valid] ** 2)))
    metrics = {
        "compare": "vtor",
        "rms_delta_vtor": rms_delta,
        "max_abs_delta_vtor": float(np.max(np.abs(delta_v[valid]))),
        "rms_delta_relative": rms_delta / rms_target if rms_target else None,
        "rms_delta_omega": float(np.sqrt(np.mean(delta_omega[valid] ** 2))),
        "psi_range": [psi_lo, psi_hi],
        "relax": factor,
        "sol_mode": mode,
        "sol_psi_start": psi_start,
        "sol_extension": v_ext_info,
    }

    prefix = str(case.flow.get("prefix", "omg_flow_match"))
    out_name = out_name or f"{prefix}_{label}.prf"
    local_dir.mkdir(parents=True, exist_ok=True)
    out_path = local_dir / out_name
    save_prf(out_path, psi_in, omega_new)

    png = paths.product_target(local_dir / f"adjust_{label}.png")
    png.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 3, figsize=(15, 4))
        v_in_p = omega_in * geom
        axes[0].plot(psi_in, v_in_p / 1e3, label=f"input omega -> v_tor ({Path(current_prf).name})")
        axes[0].plot(psi_sim, v_sim / 1e3, label="simulation carbon v_tor")
        axes[0].plot(psi_t, v_t / 1e3, "k--", label="target (CER)")
        if v_ext_info:
            ext = psi_in > psi_t.max()
            axes[0].plot(psi_in[ext], v_t_p[ext] / 1e3, "k:", label="target extrapolated (SOL)")
        axes[0].plot(psi_in, omega_new * geom / 1e3, label=f"next input -> v_tor")
        axes[0].set_xlabel("psi_n")
        axes[0].set_ylabel("v_tor [km/s]")
        axes[0].grid(True)
        axes[0].legend(fontsize=7)
        axes[1].plot(psi_in, delta_v / 1e3, label="target - sim")
        axes[1].plot(psi_in, delta_v_s / 1e3, label="smoothed")
        axes[1].axvline(psi_start, color="gray", ls=":")
        axes[1].set_xlabel("psi_n")
        axes[1].set_ylabel("delta v_tor [km/s]")
        axes[1].grid(True)
        axes[1].legend(fontsize=8)
        axes[2].plot(psi_in, omega_in / 1e3, label="omega input")
        axes[2].plot(psi_in, omega_new / 1e3, label=f"omega next ({out_name})")
        axes[2].set_xlabel("psi_n")
        axes[2].set_ylabel("omega [krad/s]")
        axes[2].grid(True)
        axes[2].legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(png, dpi=110)
    except Exception as exc:  # plotting is optional
        png = None
        metrics["plot_error"] = str(exc)

    with (local_dir / f"adjust_{label}.json").open("w", encoding="utf-8") as fh:
        json.dump({"metrics": metrics, "input_prf": str(current_prf), "next_prf": str(out_path), "sim_source": source}, fh, indent=1)
    entry = cfg.append_log(
        case,
        "adjust",
        label=label,
        input_prf=str(current_prf),
        next_prf=str(out_path),
        sim_source=source,
        metrics=metrics,
        png=str(png) if png else None,
    )
    return {"next_prf": out_path, "metrics": metrics, "png": png, "sim_source": source, "log": entry}


def _extend_target(psi_in, psi_t, v_t, sol: dict[str, Any]):
    """Target v_tor on ``psi_in`` with a gentle extrapolation past the last measured psi.

    The slope is fitted over the last ``sol.fit_width`` of the measurement and
    continued with a saturating exponential of e-folding length ``sol.width``:

        v(psi) = v_end + slope * width * (1 - exp(-(psi - psi_end) / width))

    so the extension starts with the measured slope and levels off within a
    few widths instead of running away. Inside the measurement it is the
    plain interpolation.
    """
    import numpy as np

    psi_end = float(psi_t.max())
    v_end = float(v_t[np.argmax(psi_t)])
    fit_width = float(sol.get("fit_width", 0.05))
    width = float(sol.get("width", 0.05))
    sel = psi_t >= psi_end - fit_width
    if sel.sum() >= 2:
        slope = float(np.polyfit(psi_t[sel], v_t[sel], 1)[0])
    else:
        slope = 0.0
    v_p = np.interp(psi_in, psi_t, v_t)
    ext = psi_in > psi_end
    if ext.any():
        d = psi_in[ext] - psi_end
        v_p = v_p.copy()
        v_p[ext] = v_end + slope * width * (1.0 - np.exp(-d / width))
    info = {"psi_end": psi_end, "v_end": v_end, "slope": slope, "width": width, "fit_width": fit_width, "v_asymptote": v_end + slope * width}
    return v_p, info


def _input_prf_for(case: cfg.Case, label: str) -> Path:
    """The rotation profile the run labelled ``label`` was staged with."""
    for entry in reversed(cfg.read_log(case)):
        if entry.get("stage") == "stage" and entry.get("label") == label and entry.get("prf_local"):
            return Path(entry["prf_local"])
    return case.resolve_file(case.flow["initial"])


# --- plan / stage / submit -------------------------------------------------


def _build_input(case: cfg.Case, prf_name: str, *, mstep: int | None) -> tuple[str, str, list[str]]:
    template = case.template_path.read_text(encoding="utf-8")
    param = str(case.flow.get("param", "eq_flow_file"))
    current = nle.read_value(template, param)
    if current is None:
        raise ProcedureError(f"{param} not found in template {case.template_path}")
    tokens = nle.split_array(current)
    slots = [int(s) for s in case.flow.get("slots", range(1, len(tokens) + 1))]
    value = current
    for slot in slots:
        value = nle.set_slot(value, slot, f"'{prf_name}'")
    overrides = dict(case.overrides)
    overrides.update({param: value, "sml_input_file_dir": f"'{case.data['input_file_dir']}'"})
    if mstep is not None:
        overrides["sml_mstep"] = str(int(mstep))
    text, notes = nle.apply(template, overrides)
    diff = "".join(
        difflib.unified_diff(
            template.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile="template",
            tofile="input",
        )
    )
    return text, diff, notes


def plan_iteration(
    case: cfg.Case,
    prf: Path,
    *,
    label: str | None = None,
    machine: str | None = None,
    kind: str | None = None,
    mstep: int | None = None,
    backend_name: str | None = None,
) -> dict[str, Any]:
    """Everything the next run needs, without touching the registry or a machine."""
    name, machine_cfg, _ = _machine(case, machine)
    kind = kind or case.kind
    backend = be.get_backend(backend_name or case.backend)
    label = label or cfg.next_label(case)
    prf = Path(prf)
    if not prf.exists():
        raise ProcedureError(f"rotation profile not found: {prf}")
    mstep = mstep if mstep is not None else case.data.get("mstep")
    input_text, diff, notes = _build_input(case, prf.name, mstep=mstep)
    short_name = f"{case.short_name}_{label}"
    if backend.name == "automation":
        id_plan = sr.plan_simulation_id(name, short_name, tags=["flow-match", case.name, label], kind=kind)
    else:
        id_plan = {"simulation_id": f"{case.name}_{label}", "directory_name": f"{case.name}_{label}", "backend": backend.name}
    job = case.job(name)
    return {
        "label": label,
        "machine": name,
        "backend": backend.name,
        "kind": kind,
        "short_name": short_name,
        "id_plan": id_plan,
        "prf_local": str(prf),
        "prf_remote": f"{case.remote_input_dir(name)}/{prf.name}",
        "input_text": input_text,
        "input_diff": diff,
        "input_notes": notes,
        "copy_files": case.copy_files,
        "job": job,
        "mstep": mstep,
    }


def stage_iteration(case: cfg.Case, plan: dict[str, Any], *, confirm: bool = False, overwrite_prf: bool = False) -> dict[str, Any]:
    """Reserve the simulation id, create the run directory, write the input, push the profile."""
    if not confirm:
        raise ProcedureError("stage needs confirm=True after the plan was reviewed")
    name, machine_cfg, target = _machine(case, plan["machine"])
    label = plan["label"]
    backend = be.get_backend(plan.get("backend"))
    reserved = backend.reserve(case, plan, machine_cfg)
    sim_id = reserved["simulation_id"]
    run_dir = reserved["run_dir"]
    if backend.name != "automation" and ssh.exists(target, f"{run_dir}/input"):
        raise ProcedureError(f"{run_dir} already holds an input; choose another label")

    prf_remote = plan["prf_remote"]
    if ssh.exists(target, prf_remote) and not overwrite_prf:
        remote_text = ssh.read_file(target, prf_remote)
        local_text = Path(plan["prf_local"]).read_text(encoding="utf-8")
        if remote_text != local_text:
            raise ProcedureError(f"{prf_remote} exists on {name} with different content; use overwrite_prf")
    else:
        ssh.mkdir_p(target, str(Path(prf_remote).parent))
        ssh.push(target, plan["prf_local"], prf_remote)

    ssh.mkdir_p(target, run_dir)
    ssh.write_file(target, f"{run_dir}/input", plan["input_text"])
    for fname in plan["copy_files"]:
        ssh.write_file(target, f"{run_dir}/{fname}", (case.case_dir / fname).read_text(encoding="utf-8"))
    local_dir = case.iteration_dir(label)
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "input").write_text(plan["input_text"], encoding="utf-8")

    entry = cfg.append_log(
        case,
        "stage",
        label=label,
        machine=name,
        backend=backend.name,
        kind=plan["kind"],
        simulation_id=sim_id,
        run_dir=run_dir,
        prf_local=plan["prf_local"],
        prf_remote=prf_remote,
        mstep=plan.get("mstep"),
        state="staged",
    )
    return {"simulation_id": sim_id, "run_dir": run_dir, "label": label, "log": entry}


def _iteration(case: cfg.Case, label: str) -> dict[str, Any]:
    for entry in reversed(cfg.read_log(case)):
        if entry.get("stage") == "stage" and entry.get("label") == label:
            return entry
    raise ProcedureError(f"iteration {label} has not been staged")


def submit_iteration(
    case: cfg.Case,
    label: str,
    *,
    confirm: bool = False,
    skip_dryrun: bool | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a staged iteration through ``hpc_runner.submit`` (plan only without confirm)."""
    it = _iteration(case, label)
    name = it["machine"]
    job = case.job(name)
    job.update(overrides or {})
    kwargs: dict[str, Any] = {
        k: v
        for k, v in job.items()
        if k
        in (
            "xgc_exe",
            "env_script",
            "xgc_commit",
            "nodes",
            "walltime",
            "ntasks_per_node",
            "cpus_per_task",
            "gpus_per_node",
            "account",
            "partition",
            "qos",
            "constraint",
            "spare_nodes",
            "exclude_nodes",
        )
    }
    kwargs.setdefault("job_name", f"{it['simulation_id']}_{case.name}")
    backend = be.get_backend(it.get("backend"))
    # A job whose binary cannot run on the login node (stellar-ai GPU build)
    # names a substitute in ``job.<machine>.dryrun`` (usually the CPU build of
    # the same source): the input is checked with that binary here and the
    # backend's own gate is skipped, so a rejected input still never reaches
    # the scheduler (AGENTS.md dryrun rule).
    substitute = job.get("dryrun") or {}
    dryrun_report = None
    if confirm and substitute.get("xgc_exe") and not skip_dryrun:
        from tools import dryrun as _dryrun

        ranks = (job.get("nodes") or 1) * (job.get("ntasks_per_node") or 1)
        dryrun_report = _dryrun.run_dryrun(
            name,
            it["run_dir"],
            n_ranks=ranks,
            xgc_exe=substitute["xgc_exe"],
            env_script=substitute.get("env_script"),
        )
        if dryrun_report.blocking:
            return {"submitted": False, "reason": f"input validation failed (substitute dryrun {substitute['xgc_exe']}): {dryrun_report.summary()}", "dryrun": dryrun_report.to_dict()}
        skip_dryrun = True
    result = backend.submit(case, it, kwargs, confirm=confirm, skip_dryrun=skip_dryrun)
    if dryrun_report is not None:
        result["dryrun"] = dryrun_report.to_dict()
    if confirm and result.get("submitted"):
        cfg.append_log(
            case,
            "submit",
            label=label,
            machine=name,
            backend=backend.name,
            simulation_id=it["simulation_id"],
            job_id=result.get("job_id"),
            run_id=result.get("run_id"),
            resources={k: kwargs.get(k) for k in ("nodes", "walltime", "qos", "partition", "constraint")},
            state="submitted",
        )
        cfg.update_log_entry(case, {"stage": "stage", "label": label}, state="submitted", job_id=result.get("job_id"))
        if backend.name != "automation":
            return result
        try:
            sc.set_ai_note(
                it["simulation_id"],
                f"flow_match {case.name} {label}: rotation input {Path(it['prf_local']).name}; "
                f"job {result.get('job_id')} on {name}; analyze with xgc-flowmatch after completion.",
            )
        except Exception:  # the note is informational
            pass
    return result


def refresh_status(case: cfg.Case) -> list[dict[str, Any]]:
    """Scheduler state of every submitted iteration, written back to the log."""
    out = []
    submitted = [e for e in cfg.read_log(case) if e.get("stage") == "submit" and e.get("job_id")]
    for entry in submitted:
        try:
            status = be.get_backend(entry.get("backend")).status(entry)
            state = status.get("state")
        except Exception as exc:  # unreachable machine, expired job
            status = {"error": str(exc)}
            state = None
        if state:
            cfg.update_log_entry(case, {"stage": "submit", "job_id": entry["job_id"]}, state=state)
            cfg.update_log_entry(case, {"stage": "stage", "label": entry["label"]}, state=state)
        out.append({"label": entry["label"], "simulation_id": entry["simulation_id"], "job_id": entry["job_id"], "machine": entry["machine"], "state": state, "status": status})
    return out


def history(case: cfg.Case) -> list[dict[str, Any]]:
    """One row per iteration: what was run, with what, and how far it is from the target."""
    rows: dict[str, dict[str, Any]] = {}
    for entry in cfg.read_log(case):
        label = entry.get("label")
        if not label:
            continue
        row = rows.setdefault(label, {"label": label})
        stage = entry.get("stage")
        if stage == "stage":
            row.update({k: entry.get(k) for k in ("machine", "simulation_id", "run_dir", "prf_local", "state", "job_id")})
        elif stage == "submit":
            row["job_id"] = entry.get("job_id")
            row["state"] = entry.get("state", row.get("state"))
        elif stage == "analyze":
            row["saturated"] = entry.get("saturated")
            row["window"] = entry.get("window")
        elif stage == "adjust":
            row["metrics"] = entry.get("metrics")
            row["next_prf"] = entry.get("next_prf")
    return [rows[k] for k in sorted(rows)]


def describe_job(plan_or_job: dict[str, Any]) -> str:
    job = plan_or_job.get("job", plan_or_job)
    parts = [f"{k}={job.get(k)}" for k in ("nodes", "walltime", "qos", "partition", "constraint", "xgc_exe") if job.get(k) is not None]
    return " ".join(shlex.quote(p) for p in parts)
