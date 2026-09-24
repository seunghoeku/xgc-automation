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


def dedupe_oned(od) -> int:
    """A restart re-runs the steps after its checkpoint and oneddiag keeps both
    copies; keep the last copy of every step, in step order. Returns the
    number of records dropped."""
    import numpy as np
    step = np.asarray(getattr(od, "step", getattr(od, "gstep", [])))
    n = step.shape[0] if step.ndim else 0
    if n == 0:
        return 0
    last = {}
    for i, s in enumerate(step.ravel().tolist()):
        last[s] = i
    keep = np.array([last[s] for s in sorted(last)])
    if keep.size == n and np.all(keep == np.arange(n)):
        return 0
    for name, value in list(vars(od).items()):
        if isinstance(value, np.ndarray) and value.ndim >= 1 and value.shape[0] == n:
            setattr(od, name, value[keep])
    return int(n - keep.size)


def load_run(xgc_reader, path: str):
    x = xgc_reader.xgc1(path=path)   # note: chdir into path
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()
    x.load_oned()
    dedupe_oned(x.od)
    x.load_bfield()
    x.heat_flux_all()
    return x


def particle_noise(x, directory: str, psi_min: float, psi_max: float):
    """Marker-noise measures of the written particle sample inside a psi_N band.

    ``iphase`` columns follow XGC's ptl layout (module.F90: ptl_nphase=6 then
    the constants): 0 r, 1 z, 2 phi, 3 rho_parallel, 4 w1, 5 w2, 6 mu, 7 w0,
    8 f0. w1 is the delta-f weight (fraction of the marker's full-f weight that
    is delta-f), w0 the full-f weight (physical particles per marker). Returns
    times, rms(w1*w0) [particles per marker] and rms(w1) [dimensionless]."""
    import numpy as np
    import adios2
    from matplotlib.tri import LinearTriInterpolator

    files = sorted(glob.glob(os.path.join(directory, "xgc.particle.*.bp")))
    interp = LinearTriInterpolator(x.mesh.triobj, x.mesh.psi / x.psix)
    times, rms, rms_w1 = [], [], []
    for pf in files:
        with adios2.FileReader(pf) as fh:
            iphase = fh.read("iphase")
            time = float(np.asarray(fh.read("time")).ravel()[0])
        w1 = iphase[:, 4]
        weight = w1 * iphase[:, 7]
        psi = interp(iphase[:, 0], iphase[:, 1])
        mask = np.logical_and(psi > psi_min, psi < psi_max)
        if not np.any(mask):
            continue
        times.append(time)
        rms.append(float(np.sqrt(np.mean(weight[mask] ** 2))))
        rms_w1.append(float(np.sqrt(np.mean(w1[mask] ** 2))))
    return times, rms, rms_w1


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
    parser.add_argument("--paper", action="store_true",
                        help="publication styling: font 28, 13x10 in figures, linewidth 3, dpi 180")
    parser.add_argument("--twod-scale", choices=["shared", "per-panel"], default="shared",
                        help="colour range of the (psi_N, t) panels: the Maxwellian run's for all, "
                             "or each panel's own (labelled colour bars either way)")
    parser.add_argument("--twod-runs", nargs="*", default=None,
                        help="run names (directory basenames, 'maxwell' for stage A) for the 2-D "
                             "figure; default all. Two panels are labelled (a)/(b) like the paper")
    parser.add_argument("--highlight", action="append", default=[],
                        help="run name (directory basename) drawn thicker and on top (repeatable)")
    parser.add_argument("--vort-inset-ylim", type=float, nargs=2, default=None,
                        help="fixed y range of the vorticity-flux inset (default: the data)")
    parser.add_argument("--shear-inset-ylim", type=float, nargs=2, default=None,
                        help="fixed y range of the ExB-shear inset [kHz] (default: the data)")
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
            times, rms, rms_w1 = particle_noise(x, d, *args.psi_band)
            runs[-1]["noise_time_ms"] = [(t + plot_offset) * s2ms for t in times]
            runs[-1]["noise_rms"] = rms
            runs[-1]["noise_rms_w1"] = rms_w1
    if not args.no_particles:
        times, rms, rms_w1 = particle_noise(x1, run_dir, *args.psi_band)
        runs[0]["noise_time_ms"] = [t * s2ms for t in times]
        runs[0]["noise_rms"] = rms
        runs[0]["noise_rms_w1"] = rms_w1

    # --- figures ----------------------------------------------------------
    font, figsize, lw, dpi, legend_fs = (28, (13, 10), 3.0, 180, 24) if args.paper else (16, (9, 6.5), 2.2, 150, 13)
    plt.rcParams.update({"font.size": font})
    outputs = {}

    def parse_style(value):
        """Accept 'dashdot' / '-.' or a JSON dash tuple '(0, (3, 1, 1, 1))'."""
        if isinstance(value, str) and value.startswith("("):
            import ast
            return ast.literal_eval(value)
        return value

    for r in runs:
        r["style"] = parse_style(r["style"])

    def line_kw(r, width):
        """Line width and stacking: highlighted runs 1.4x wider, drawn last."""
        hi = r["name"] in args.highlight
        return {"linewidth": width * (1.4 if hi else 1.0), "zorder": 10 if hi else 2}

    def draw_series(ax, key, xlim=None, lw_=None):
        """Plot every run on ``ax``; returns the values inside ``xlim``."""
        inside = []
        for i, r in enumerate(runs):
            t = np.asarray(r["time_ms"])
            v = np.asarray(r[key])
            m = np.ones_like(t, dtype=bool) if xlim is None else (t >= xlim[0]) & (t <= xlim[1])
            if not np.any(m):
                continue
            inside.extend(v[m].tolist())
            ax.plot(t, v, label=r["label"], linestyle=r["style"], color=r["color"], **line_kw(r, lw_ or lw))
        if xlim:
            ax.set_xlim(*xlim)
            if inside:
                pad = 0.1 * (max(inside) - min(inside)) or 1.0
                ax.set_ylim(min(inside) - pad, max(inside) + pad)
        return inside

    def series_figure(key, ylabel, title, fname, xlim=None, loc="best", inset=None, ylim_top=None,
                      legend_below=False):
        """One trace per run; with ``xlim`` the y range follows the data inside
        the window and runs without samples there stay out of the legend.
        ``inset`` = (t0, t1, bounds) magnifies that window inside the axes;
        ``ylim_top`` raises the upper limit so the inset sits above the data."""
        fig, ax = plt.subplots(figsize=figsize)
        inside = []
        for i, r in enumerate(runs):
            t = np.asarray(r["time_ms"])
            v = np.asarray(r[key])
            m = np.ones_like(t, dtype=bool) if xlim is None else (t >= xlim[0]) & (t <= xlim[1])
            if not np.any(m):
                continue
            inside.extend(v[m].tolist())
            ax.plot(t, v, label=r["label"], linestyle=r["style"], color=r["color"], **line_kw(r, lw))
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if xlim:
            ax.set_xlim(*xlim)
            if inside:
                pad = 0.1 * (max(inside) - min(inside)) or 1.0
                ax.set_ylim(min(inside) - pad, max(inside) + pad)
        if ylim_top is not None:
            lo, hi = ax.get_ylim()
            ax.set_ylim(lo, max(hi, ylim_top))
        if legend_below:
            # outside the axes, two columns under the x label
            handles, labels_ = ax.get_legend_handles_labels()
            fig.legend(handles, labels_, loc="lower center", ncol=2, fontsize=int(legend_fs * 0.9),
                       frameon=False, bbox_to_anchor=(0.5, 0.0))
            nrow = -(-len(labels_) // 2)
            fig.set_size_inches(figsize[0], figsize[1] * (1 + 0.05 * nrow))
        else:
            ax.legend(loc=loc, fontsize=legend_fs)
        if inset is not None:
            t0, t1, bounds = inset
            from matplotlib.ticker import MaxNLocator
            axins = ax.inset_axes(bounds)
            draw_series(axins, key, xlim=(t0, t1), lw_=lw * 0.7)
            axins.xaxis.set_major_locator(MaxNLocator(4))
            axins.yaxis.set_major_locator(MaxNLocator(4))
            axins.tick_params(labelsize=int(font * 0.6))
            axins.set_title(f"{t0:.2f} - {t1:.2f} ms", fontsize=int(font * 0.65))
            ax.indicate_inset_zoom(axins, edgecolor="0.3")
        if legend_below:
            nrow = -(-len(ax.get_legend_handles_labels()[1]) // 2)
            fig.tight_layout(rect=(0, 0.05 * nrow / (1 + 0.05 * nrow), 1, 1))
        else:
            fig.tight_layout()
        path = f"{out_prefix}_{fname}.png"
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        outputs[fname] = path

    def make_room(ax, key, t_from, frac, top):
        """Stretch the y range so every run stays clear of an inset that
        covers the axes fraction above ``frac`` (``top``) or below it, for
        t >= ``t_from``."""
        lo, hi = ax.get_ylim()
        vals = []
        for r in runs:
            t = np.asarray(r["time_ms"])
            v = np.asarray(r[key])
            m = t >= t_from
            vals.extend(v[m].tolist())
        if not vals:
            return
        if top:
            vmax = max(vals)
            need = lo + (vmax - lo) / frac
            ax.set_ylim(lo, max(hi, need))
        else:
            vmin = min(vals)
            need = hi - (hi - vmin) / (1.0 - frac)
            ax.set_ylim(min(lo, need), hi)

    def combined_figure(win):
        """Paper Fig. GAM as one figure: (a) vorticity flux, (b) ExB shear,
        side by side, one legend below both panels, enlarged insets over the
        first 0.2 ms after the f_init start."""
        from matplotlib.ticker import MaxNLocator
        t0, t1 = win
        panels = [
            ("vort_flux", "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)",
             f"(a) Radial Vorticity Flux at {psi_txt}", True, args.vort_inset_ylim),
            ("shear_khz", "ExB shear (kHz)", f"(b) ExB shear at {psi_txt}", False, args.shear_inset_ylim),
        ]
        size = (26, 11.5) if args.paper else (17, 7)
        fig, axes = plt.subplots(1, 2, figsize=size)
        for ax, (key, ylabel, title, inset_top, inset_ylim) in zip(axes, panels):
            draw_series(ax, key)
            ax.set_xlabel("Time (ms)")
            ax.set_ylabel(ylabel)
            ax.set_title(title)
            t_all = [r["time_ms"] for r in runs]
            ax.set_xlim(0.0, max(t[-1] for t in t_all))
            # inset spans the axes from x-fraction 0.22 to the right edge and
            # half the height; the data after its left edge is kept outside it
            x0, w, h = 0.22, 0.76, 0.46
            y0 = 0.51 if inset_top else 0.09
            xl = ax.get_xlim()
            t_from = xl[0] + x0 * (xl[1] - xl[0])
            make_room(ax, key, t_from, y0 - 0.04 if inset_top else y0 + h + 0.04, inset_top)
            axins = ax.inset_axes([x0, y0, w, h])
            draw_series(axins, key, xlim=(t0, t1), lw_=lw * 0.75)
            if inset_ylim is not None:
                # a fixed range keeps the late small oscillations readable
                # when one run's first swing is much larger (clipped)
                axins.set_ylim(*inset_ylim)
            axins.xaxis.set_major_locator(MaxNLocator(5))
            axins.yaxis.set_major_locator(MaxNLocator(4))
            axins.tick_params(labelsize=int(font * 0.7))
            axins.text(0.99, 0.96 if inset_top else 0.04, f"{t0:.2f} - {t1:.2f} ms",
                       transform=axins.transAxes, ha="right", va="top" if inset_top else "bottom",
                       fontsize=int(font * 0.72))
            ax.indicate_inset_zoom(axins, edgecolor="0.3")
        handles, labels_ = axes[0].get_legend_handles_labels()
        ncol = 4 if len(labels_) > 4 else len(labels_)
        fig.legend(handles, labels_, loc="lower center", ncol=ncol, fontsize=legend_fs,
                   frameon=False, bbox_to_anchor=(0.5, 0.0))
        nrow = -(-len(labels_) // ncol)
        fig.tight_layout(rect=(0, 0.03 + 0.05 * nrow, 1, 1))
        path = f"{out_prefix}_gam_combined.png"
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        outputs["gam_combined"] = path

    psi_txt = f"$\\psi_N={psi_at:3.2f}$"
    # With the f_init runs shifted, the referee's inset (0.3-0.5 ms) sits over
    # the f_init start; without a shift the zoom figure below plays that role.
    inset_win = (plot_offset * s2ms, plot_offset * s2ms + 0.2) if args.shift_finit else None
    # The inset lives in the empty band above the Maxwellian relaxation
    # (t > 0.15 ms, flux > 2.5): the y range is raised to 5 to make room.
    series_figure("vort_flux", "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)",
                  f"(a) Radial Vorticity Flux at {psi_txt}", "vorticity", loc="lower right",
                  inset=None if inset_win is None else (*inset_win, [0.30, 0.62, 0.67, 0.29]),
                  ylim_top=None if inset_win is None else 5.0)
    series_figure("shear_khz", "ExB shear (kHz)", f"(b) ExB shear at {psi_txt}", "exb_shear",
                  loc="upper right",
                  inset=None if inset_win is None else (*inset_win, [0.40, 0.10, 0.57, 0.30]))
    series_figure("eflux_mw", "Energy Flux (MW)", f"Radial Energy Flux at {psi_txt}", "energy_flux")

    if inset_win is not None:
        combined_figure(inset_win)

    t_end = max(r["time_ms"][-1] for r in runs)
    win = (t_end - args.end_window, t_end)
    series_figure("shear_khz", "ExB shear (kHz)", f"ExB shear at {psi_txt}, t = {win[0]:.2f} - {win[1]:.2f} ms",
                  "exb_shear_end", xlim=win, legend_below=True)

    # zoom of the transient right after the f_init start
    t_first = plot_offset * s2ms
    series_figure("vort_flux", "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)",
                  f"Radial Vorticity Flux at {psi_txt}, first 0.15 ms",
                  "vorticity_zoom", xlim=(t_first - 0.005, t_first + 0.15), loc="lower right")

    # 2-D vorticity flux: Maxwellian vs every f_init start, one colour scale
    # colour scale from the Maxwellian run (the paper's choice); every panel
    # uses the same explicit levels so the colour bar applies to all of them.
    z0 = np.asarray(runs[0]["vort_flux_2d"])
    vlim0 = float(np.max(np.abs(z0)))
    levels = np.linspace(-vlim0, vlim0, 101)
    cbar_label = "Vorticity Flux ($10^{-10}$ $m^3 s^{-2}$)"
    twod = runs if args.twod_runs is None else [r for r in runs if r["name"] in args.twod_runs]
    if args.twod_runs is not None and len(twod) != len(args.twod_runs):
        known = [r["name"] for r in runs]
        raise SystemExit(f"--twod-runs: unknown run among {args.twod_runs}; known: {known}")
    ncol = len(twod)
    paper_2d = args.paper and ncol == 2
    with plt.rc_context({"font.size": 19 if paper_2d else font}):
        fig, axes = plt.subplots(1, ncol, figsize=(14, 7) if paper_2d else (4.4 * ncol, 6.5),
                                 sharey=paper_2d)
        axes = np.atleast_1d(axes)
        if not paper_2d:
            fig.subplots_adjust(wspace=0.45)
        span = 0.3   # ms shown after each start
        for k, (ax, r) in enumerate(zip(axes, twod)):
            z = np.asarray(r["vort_flux_2d"])
            lv = levels
            if args.twod_scale == "per-panel":
                span_t = 0.3
                t_arr = np.asarray(r["time_ms"])
                t0_ = 0.0 if r["name"] == "maxwell" else t_first
                win = (t_arr >= t0_) & (t_arr <= t0_ + span_t)
                vl = float(np.max(np.abs(z[win]))) if np.any(win) else vlim0
                lv = np.linspace(-vl, vl, 101)
            z = np.clip(z, lv[0], lv[-1])
            cf = ax.contourf(psi_1d, np.asarray(r["time_ms"]), z, levels=lv, cmap="RdBu_r")
            ax.set_xlabel("$\\psi_N$")
            if paper_2d:
                ax.set_title(f"({'ab'[k]}) " + ("From Maxwellian" if r["name"] == "maxwell" else "Neoclassical Start"))
            else:
                ax.set_title(r["label"], fontsize=13)
            ax.set_xlim(0.0, 0.8)
            t0 = 0.0 if r["name"] == "maxwell" else t_first
            ax.set_ylim(t0, t0 + span)
            if t_first == 0.0 and ax is not axes[0] and not paper_2d:
                ax.tick_params(labelleft=False)
            if paper_2d or args.twod_scale == "per-panel":
                cb = fig.colorbar(cf, ax=ax)
                cb.set_label(cbar_label, fontsize=int((19 if paper_2d else font) * 0.8))
        axes[0].set_ylabel("Time (ms)")
        if not (paper_2d or args.twod_scale == "per-panel"):
            fig.colorbar(cf, ax=axes.tolist(), shrink=0.8, label=cbar_label)
        path = f"{out_prefix}_vorticity_2d.png"
        fig.savefig(path, dpi=dpi if paper_2d else 130, bbox_inches="tight")
        plt.close(fig)
    outputs["vorticity_2d"] = path

    if not args.no_particles:
        fig, ax = plt.subplots(figsize=figsize)
        for i, r in enumerate(runs):
            if r.get("noise_time_ms"):
                ax.plot(r["noise_time_ms"], np.asarray(r["noise_rms"]) * 1e-12, label=r["label"],
                        linestyle=r["style"], color=r["color"], **line_kw(r, lw))
        if not args.shift_finit:
            ax.set_xlim(0.0, min(r["time_ms"][-1] for r in runs))
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel("RMS $w_1 w_0$ ($10^{12}$ ions per marker)")
        ax.set_title(f"Particle noise, {args.psi_band[0]} < $\\psi_N$ < {args.psi_band[1]}")
        ax.set_ylim(bottom=0.0)
        # the curves sit in the upper half after the first 0.05 ms; two
        # columns along the bottom keep the legend off every line
        ax.legend(fontsize=int(legend_fs * 0.85), ncol=2, loc="lower center")
        fig.tight_layout()
        path = f"{out_prefix}_noise.png"
        fig.savefig(path, dpi=dpi)
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
            "noise_rms_w1_last": (r["noise_rms_w1"][-1] if r.get("noise_rms_w1") else None),
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
