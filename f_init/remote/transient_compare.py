#!/usr/bin/env python3
"""Login-node step: initial-transient comparison of f_init production runs.

Reproduces the CBC figures of the neoclassical-initialisation paper
(2026_neo_init, Sec. "Axisymmetric test and poloidal smoothing") and the
``cbc3_f_init_plots.ipynb`` notebook for one f_init case:

* radial vorticity flux  -<Gamma_i> * e B / (2 m_p n_i0)  at one psi_N
* flux-surface-averaged ExB shear at the same psi_N (kHz)
* the same shear magnified over the last part of the runs
* vorticity flux as a function of psi_N and time (Maxwellian vs f_init starts)
* radial ion energy flux at the same psi_N (MW), as in the procedure notebook
* particle noise: rms of the delta-f marker weight from xgc.particle.*.bp

Every run is plotted from t = 0, as in the paper, so the transients can be
compared directly. With ``--shift-finit`` the f_init runs are instead shifted
by the time at which the distribution was extracted (``--f0-step``, default
the last f0 step of the Maxwellian run) and continue the precursor curve; the
extraction time is recorded in the JSON either way. Reduced
series are written to ``<out-prefix>.json`` and figures to
``<out-prefix>_<name>.png``. Last stdout line: ``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def last_f0_step(directory: str) -> int | None:
    steps = []
    for path in glob.glob(os.path.join(directory, "xgc.f0.[0-9]*.bp")):
        match = re.search(r"xgc\.f0\.(\d+)\.bp$", path)
        if match:
            steps.append(int(match.group(1)))
    return max(steps) if steps else None


def load_run(xgc_reader, path: str):
    x = xgc_reader.xgc1(path=path)   # note: chdir into path
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()
    x.load_oned()
    x.load_bfield()
    x.heat_flux_all()
    return x


def particle_noise(x, directory: str, psi_min: float, psi_max: float):
    """rms(delta-f weight) of the written marker sample inside a psi_N band."""
    import numpy as np
    import adios2
    from matplotlib.tri import LinearTriInterpolator

    files = sorted(glob.glob(os.path.join(directory, "xgc.particle.*.bp")))
    interp = LinearTriInterpolator(x.mesh.triobj, x.mesh.psi / x.psix)
    times, rms = [], []
    for pf in files:
        with adios2.FileReader(pf) as fh:
            iphase = fh.read("iphase")
            time = float(np.asarray(fh.read("time")).ravel()[0])
        weight = iphase[:, 4] * iphase[:, 7]
        psi = interp(iphase[:, 0], iphase[:, 1])
        mask = np.logical_and(psi > psi_min, psi < psi_max)
        if not np.any(mask):
            continue
        times.append(time)
        rms.append(float(np.sqrt(np.mean(weight[mask] ** 2))))
    return times, rms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="Maxwellian-start XGCa run")
    parser.add_argument("--finit-dir", action="append", default=[],
                        help="production run started from an f_init file (repeatable)")
    parser.add_argument("--label", action="append", default=[],
                        help="legend label per --finit-dir, same order")
    parser.add_argument("--color", action="append", default=[],
                        help="matplotlib colour per --finit-dir, same order (default: cycle)")
    parser.add_argument("--style", action="append", default=[],
                        help="matplotlib linestyle per --finit-dir, same order (default: cycle)")
    parser.add_argument("--f0-step", type=int, default=None,
                        help="step the f_init came from; default last f0 step of run-dir")
    parser.add_argument("--psi", type=float, default=0.45, help="psi_N of the 1-D traces")
    parser.add_argument("--end-window", type=float, default=0.04,
                        help="width [ms] of the magnified shear window at the end")
    parser.add_argument("--psi-band", type=float, nargs=2, default=(0.1, 0.8),
                        help="psi_N band for the particle-noise rms")
    parser.add_argument("--shift-finit", action="store_true",
                        help="shift f_init runs by the f0 extraction time instead of starting at 0")
    parser.add_argument("--no-particles", action="store_true")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    if len(args.label) not in (0, len(args.finit_dir)):
        raise SystemExit("--label must be given once per --finit-dir or not at all")
    labels = args.label or [os.path.basename(d.rstrip("/")) for d in args.finit_dir]
    for opt, name in ((args.color, "--color"), (args.style, "--style")):
        if len(opt) not in (0, len(args.finit_dir)):
            raise SystemExit(f"{name} must be given once per --finit-dir or not at all")

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from xgc_reader import base as xgc_reader

    run_dir = os.path.abspath(args.run_dir)
    finit_dirs = [os.path.abspath(d) for d in args.finit_dir]
    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)

    missing = [d for d in [run_dir] + finit_dirs if not os.path.exists(os.path.join(d, "xgc.oneddiag.bp"))]
    if missing:
        emit({"error": "missing", "paths": [os.path.join(d, "xgc.oneddiag.bp") for d in missing]})
        return 2

    f0_step = args.f0_step if args.f0_step is not None else last_f0_step(run_dir)
    if f0_step is None:
        emit({"error": "missing", "paths": [os.path.join(run_dir, "xgc.f0.*.bp")]})
        return 2

    x1 = load_run(xgc_reader, run_dir)
    od1 = x1.od
    psi_1d = np.asarray(od1.psi)
    ip = int(np.argmin(np.abs(psi_1d - args.psi)))
    psi_at = float(psi_1d[ip])

    # -<pflux> e B / (2 m_p n_i0): particle flux -> vorticity flux (notebook cell 8)
    bmag = np.sqrt(x1.bfield[0, :] ** 2 + x1.bfield[1, :] ** 2 + x1.bfield[2, :] ** 2)
    n2vort = np.asarray(x1.fsa_simple(x1.cnst.echarge * bmag / x1.cnst.protmass / 2 / x1.f0.ni0))

    steps1 = np.asarray(od1.step)
    hit = np.where(steps1 == f0_step)[0]
    t_offset = float(od1.time[hit[0]]) if len(hit) else float(od1.time[-1])
    plot_offset = t_offset if args.shift_finit else 0.0

    s2ms, mw, vnorm, khz = 1e3, 1e-6, 1e-10, 1e-3

    def reduce(x, offset):
        od = x.od
        t = (np.asarray(od.time) + offset) * s2ms
        return {
            "time_ms": t.tolist(),
            "eflux_mw": (np.asarray(od.efluxi)[:, ip] * mw).tolist(),
            "vort_flux": (-np.asarray(od.pfluxi)[:, ip] * n2vort[ip] * vnorm).tolist(),
            "shear_khz": (np.asarray(od.shear_r)[:, ip] * khz).tolist(),
            "vort_flux_2d": (-np.asarray(od.pfluxi) * n2vort[None, :] * vnorm).tolist(),
        }

    default_styles = ["-", "-.", "--", "-", ":", (0, (3, 1, 1, 1, 1, 1)), (0, (5, 2))]
    runs = [{"name": "maxwell", "label": "From Maxwellian", "dir": run_dir,
             "color": "C0", "style": "-", **reduce(x1, 0.0)}]
    for i, (d, lbl) in enumerate(zip(finit_dirs, labels), start=1):
        x = load_run(xgc_reader, d)
        runs.append({"name": os.path.basename(d), "label": lbl, "dir": d,
                     "color": args.color[i - 1] if args.color else f"C{i}",
                     "style": args.style[i - 1] if args.style else default_styles[i % len(default_styles)],
                     **reduce(x, plot_offset)})
        if not args.no_particles:
            times, rms = particle_noise(x, d, *args.psi_band)
            runs[-1]["noise_time_ms"] = [(t + plot_offset) * s2ms for t in times]
            runs[-1]["noise_rms"] = rms
    if not args.no_particles:
        times, rms = particle_noise(x1, run_dir, *args.psi_band)
        runs[0]["noise_time_ms"] = [t * s2ms for t in times]
        runs[0]["noise_rms"] = rms

    # --- figures ----------------------------------------------------------
    plt.rcParams.update({"font.size": 16})
    outputs = {}

    def parse_style(value):
        """Accept 'dashdot' / '-.' or a JSON dash tuple '(0, (3, 1, 1, 1))'."""
        if isinstance(value, str) and value.startswith("("):
            import ast
            return ast.literal_eval(value)
        return value

    for r in runs:
        r["style"] = parse_style(r["style"])

    def series_figure(key, ylabel, title, fname, xlim=None, loc="best"):
        """One trace per run; with ``xlim`` the y range follows the data inside
        the window and runs without samples there stay out of the legend."""
        fig, ax = plt.subplots(figsize=(9, 6.5))
        inside = []
        for i, r in enumerate(runs):
            t = np.asarray(r["time_ms"])
            v = np.asarray(r[key])
            m = np.ones_like(t, dtype=bool) if xlim is None else (t >= xlim[0]) & (t <= xlim[1])
            if not np.any(m):
                continue
            inside.extend(v[m].tolist())
            ax.plot(t, v, label=r["label"], linewidth=2.2, linestyle=r["style"], color=r["color"])
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if xlim:
            ax.set_xlim(*xlim)
            if inside:
                pad = 0.1 * (max(inside) - min(inside)) or 1.0
                ax.set_ylim(min(inside) - pad, max(inside) + pad)
        ax.legend(loc=loc, fontsize=13)
        fig.tight_layout()
        path = f"{out_prefix}_{fname}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        outputs[fname] = path

    psi_txt = f"$\\psi_N={psi_at:3.2f}$"
    series_figure("vort_flux", "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)",
                  f"(a) Radial Vorticity Flux at {psi_txt}", "vorticity", loc="lower right")
    series_figure("shear_khz", "ExB shear (kHz)", f"(b) ExB shear at {psi_txt}", "exb_shear")
    series_figure("eflux_mw", "Energy Flux (MW)", f"Radial Energy Flux at {psi_txt}", "energy_flux")

    t_end = max(r["time_ms"][-1] for r in runs)
    win = (t_end - args.end_window, t_end)
    series_figure("shear_khz", "ExB shear (kHz)", f"ExB shear at {psi_txt}, t = {win[0]:.2f} - {win[1]:.2f} ms",
                  "exb_shear_end", xlim=win)

    # zoom of the transient right after the f_init start
    t_first = plot_offset * s2ms
    series_figure("vort_flux", "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)",
                  f"Radial Vorticity Flux at {psi_txt}, first 0.15 ms",
                  "vorticity_zoom", xlim=(t_first - 0.005, t_first + 0.15), loc="lower right")

    # 2-D vorticity flux: Maxwellian vs every f_init start, one colour scale
    # colour scale from the Maxwellian run (the paper's choice); every panel
    # uses the same explicit levels so the colour bar applies to all of them.
    z0 = np.asarray(runs[0]["vort_flux_2d"])
    levels = np.linspace(float(z0.min()), float(z0.max()), 101)
    ncol = len(runs)
    fig, axes = plt.subplots(1, ncol, figsize=(4.4 * ncol, 6.5))
    axes = np.atleast_1d(axes)
    fig.subplots_adjust(wspace=0.45)
    span = 0.3   # ms shown after each start
    for ax, r in zip(axes, runs):
        z = np.clip(np.asarray(r["vort_flux_2d"]), levels[0], levels[-1])
        cf = ax.contourf(psi_1d, np.asarray(r["time_ms"]), z, levels=levels, cmap="viridis")
        ax.set_xlabel("$\\psi_N$")
        ax.set_title(r["label"], fontsize=13)
        ax.set_xlim(0.0, 0.8)
        t0 = 0.0 if r["name"] == "maxwell" else t_first
        ax.set_ylim(t0, t0 + span)
        if t_first == 0.0 and ax is not axes[0]:
            ax.tick_params(labelleft=False)
    axes[0].set_ylabel("Time (ms)")
    fig.colorbar(cf, ax=axes.tolist(), shrink=0.8, label="Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)")
    path = f"{out_prefix}_vorticity_2d.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    outputs["vorticity_2d"] = path

    if not args.no_particles:
        fig, ax = plt.subplots(figsize=(9, 6.5))
        for i, r in enumerate(runs):
            if r.get("noise_time_ms"):
                ax.plot(r["noise_time_ms"], np.asarray(r["noise_rms"]) * 1e-12, label=r["label"],
                        linewidth=2, linestyle=r["style"], color=r["color"])
        ax.set_xlabel("time [ms]")
        ax.set_ylabel("RMS particle weights (A.U.)")
        ax.set_title(f"Particle noise, {args.psi_band[0]} < $\\psi_N$ < {args.psi_band[1]}")
        ax.legend(fontsize=13)
        fig.tight_layout()
        path = f"{out_prefix}_noise.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        outputs["noise"] = path

    # --- summary metrics ---------------------------------------------------
    def peak_after_start(r, key):
        t = np.asarray(r["time_ms"])
        v = np.asarray(r[key])
        m = t <= (0.15 if r["name"] == "maxwell" else t_first + 0.15)
        return float(np.max(np.abs(v[m]))) if np.any(m) else None

    def end_rms(r, key):
        t = np.asarray(r["time_ms"])
        v = np.asarray(r[key])
        m = t >= win[0]
        return float(np.std(v[m])) if np.any(m) else None

    summary = []
    for r in runs:
        summary.append({
            "name": r["name"], "label": r["label"],
            "vort_flux_peak": peak_after_start(r, "vort_flux"),
            "shear_peak_khz": peak_after_start(r, "shear_khz"),
            "eflux_peak_mw": peak_after_start(r, "eflux_mw"),
            "shear_end_std_khz": end_rms(r, "shear_khz"),
            "noise_rms_last": (r["noise_rms"][-1] if r.get("noise_rms") else None),
            "t_end_ms": r["time_ms"][-1],
        })

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"psi_index": ip, "psi_at": psi_at, "f0_step": f0_step,
                   "t_offset_ms": t_offset * s2ms, "shift_finit": bool(args.shift_finit),
                   "psi_1d": psi_1d.tolist(),
                   "runs": runs, "summary": summary}, fh)

    emit({"psi_index": ip, "psi_at": psi_at, "f0_step": f0_step, "t_offset_ms": t_offset * s2ms,
          "shift_finit": bool(args.shift_finit),
          "json": json_path, "png": outputs, "summary": summary})
    return 0


if __name__ == "__main__":
    sys.exit(main())
