#!/usr/bin/env python3
"""Login-node step: (psi_N, frequency) spectrum of the axisymmetric transient.

The radial vorticity flux -<Gamma_i> e B / (m_i n_i0) and the flux-surface-
averaged ExB shear are read from ``xgc.oneddiag.bp`` (restart duplicates
removed), a window of the run is Fourier transformed in time at every psi
(slow drift removed by subtracting a Gaussian running mean of width
--drift-width, Hann window), and |FFT| normalised to its maximum at each psi
is drawn with the analytic GAM frequency on top:

  Winsor (fluid):          f = sqrt((7/4 + tau) 2 T_i / m_i) / (2 pi R_0)
  Sugama-Watanabe (kin.):  f_W * sqrt(1 + 2 (23 + 16 tau + 4 tau^2) / (q^2 (7 + 4 tau)^2))

with T_i from the first oneddiag step, tau = T_e/T_i (argument), q from
xgc.mesh.bp and R_0 the magnetic-axis major radius. Last stdout line:
``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--t-min", type=float, default=0.0, help="window start [ms]")
    parser.add_argument("--t-max", type=float, default=0.31, help="window end [ms]")
    parser.add_argument("--drift-width", type=float, default=0.025,
                        help="Gaussian width [ms] of the running mean removed before the FFT")
    parser.add_argument("--tau", type=float, default=1.0, help="T_e/T_i")
    parser.add_argument("--psi-min", type=float, default=0.05)
    parser.add_argument("--psi-max", type=float, default=0.95)
    parser.add_argument("--f-max", type=float, default=150.0, help="top of the frequency axis [kHz]")
    parser.add_argument("--signals", default="vort,shear", help="panels: comma list of vort, shear")
    parser.add_argument("--models", default="winsor,sugama", help="overlays: comma list of winsor, sugama")
    parser.add_argument("--suptitle", action="store_true", help="add a title with the time window")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.ndimage import gaussian_filter1d
    from xgc_reader import base as xgc_reader

    run_dir = os.path.abspath(args.run_dir)
    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)

    x = xgc_reader.xgc1(path=run_dir)
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()
    x.load_oned()
    od = x.od
    steps = np.asarray(od.step)
    _, first_rev = np.unique(steps[::-1], return_index=True)
    keep = np.sort(len(steps) - 1 - first_rev)
    x.load_bfield()
    x.heat_flux_all()

    t = np.asarray(od.time)[keep] * 1e3                     # ms
    psi = np.asarray(od.psi)
    psi = psi[0] if psi.ndim == 2 else psi
    mass = float(x.unit_dic["i_ptl_mass_au"]) * x.cnst.protmass
    bmag = np.sqrt(x.bfield[0, :] ** 2 + x.bfield[1, :] ** 2 + x.bfield[2, :] ** 2)
    n2vort = np.asarray(x.fsa_simple(x.cnst.echarge * bmag / mass / x.f0.ni0))
    series = {
        "vort": -np.asarray(od.pfluxi)[keep] * n2vort[None, :],   # m^3 s^-2
        "shear": np.asarray(od.shear_r)[keep],                    # s^-1
    }

    win = (t >= args.t_min) & (t <= args.t_max)
    tw = t[win]
    dt = float(np.median(np.diff(tw))) * 1e-3               # s
    nfft = len(tw)
    freq = np.fft.rfftfreq(nfft, dt) * 1e-3                 # kHz
    hann = np.hanning(nfft)
    spec = {}
    for key, z in series.items():
        zw = z[win] - gaussian_filter1d(z[win], args.drift_width * 1e-3 / dt, axis=0, mode="nearest")
        a = np.abs(np.fft.rfft(zw * hann[:, None], axis=0))
        a[0] = 0.0
        spec[key] = a / np.maximum(a.max(axis=0, keepdims=True), 1e-300)

    # analytic GAM frequency from the initial profile
    ti_ev = np.asarray(od.Ti)[keep][0]
    vti2 = 2.0 * ti_ev * x.cnst.echarge / mass
    r0 = float(x.eq_axis_r)
    tau = args.tau
    f_w = np.sqrt((7.0 / 4.0 + tau) * vti2) / (2 * np.pi * r0) * 1e-3
    psi_surf = x.mesh.psi_surf / x.psix
    closed = np.nonzero(np.logical_and(psi_surf[:-1] <= 1, psi_surf[1:] > 1))[0]
    m = slice(0, closed[0] if len(closed) else len(psi_surf))
    order = np.argsort(psi_surf[m])
    q = np.interp(psi, psi_surf[m][order], np.asarray(x.mesh.qsafety)[m][order])
    f_sw = f_w * np.sqrt(1 + 2 * (23 + 16 * tau + 4 * tau ** 2) / (q ** 2 * (7 + 4 * tau) ** 2))

    sel = (psi >= args.psi_min) & (psi <= args.psi_max)
    fsel = freq <= args.f_max
    band = freq >= 10.0                                     # peak search above the slow drift
    peak = {k: freq[band][np.argmax(v[band], axis=0)] for k, v in spec.items()}

    keys = [k for k in args.signals.split(",") if k]
    models = [m for m in args.models.split(",") if m]
    font = 22 if args.paper else 14
    figsize = (8.5 * len(keys) - 0.5 * (len(keys) - 1), 7.5) if args.paper else (7 * len(keys), 6)
    dpi = 180 if args.paper else 150
    plt.rcParams.update({"font.size": font})
    fig, axes = plt.subplots(1, len(keys), figsize=figsize, sharey=True, squeeze=False)
    axes = axes[0]
    names = {"vort": "Radial vorticity flux", "shear": "$E\\times B$ shear"}
    for k, (ax, key) in enumerate(zip(axes, keys)):
        pc = ax.pcolormesh(psi[sel], freq[fsel], spec[key][fsel][:, sel], shading="auto",
                           cmap="viridis", vmin=0, vmax=1)
        if "winsor" in models:
            ax.plot(psi[sel], f_w[sel], "w--", lw=2.5, label="Winsor (fluid)")
        if "sugama" in models:
            ax.plot(psi[sel], f_sw[sel], "r-", lw=2, label="Sugama-Watanabe")
        ax.set_xlabel("$\\psi_N$")
        ax.set_title((f"({'ab'[k]}) " if len(keys) > 1 else "") + names[key])
        ax.set_xlim(args.psi_min, args.psi_max)
        ax.set_ylim(0, args.f_max)
    axes[0].set_ylabel("Frequency (kHz)")
    axes[-1].legend(loc="upper right", fontsize=int(font * 0.75))
    cb = fig.colorbar(pc, ax=list(axes), pad=0.02)
    cb.set_label("|FFT| / max at each $\\psi_N$")
    if args.suptitle:
        fig.suptitle(f"Maxwellian start, t = {tw[0]:.2f}-{tw[-1]:.2f} ms", fontsize=font)
    png = f"{out_prefix}.png"
    fig.savefig(png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"psi_n": psi[sel].tolist(), "freq_khz": freq[fsel].tolist(),
                   "window_ms": [float(tw[0]), float(tw[-1])], "dt_s": dt, "drift_width_ms": args.drift_width, "tau": tau, "R0": r0,
                   "f_winsor_khz": f_w[sel].tolist(), "f_sugama_khz": f_sw[sel].tolist(),
                   "q": q[sel].tolist(), "ti_ev": ti_ev[sel].tolist(),
                   "peak_khz": {k: v[sel].tolist() for k, v in peak.items()},
                   "spectrum": {k: v[fsel][:, sel].tolist() for k, v in spec.items()}}, fh)
    samples = {}
    for p0 in (0.2, 0.45, 0.7):
        i = int(np.argmin(np.abs(psi - p0)))
        samples[f"{p0}"] = {"psi": float(psi[i]), "q": float(q[i]), "ti_ev": float(ti_ev[i]),
                            "f_winsor": float(f_w[i]), "f_sugama": float(f_sw[i]),
                            "peak_vort": float(peak["vort"][i]), "peak_shear": float(peak["shear"][i])}
    emit({"json": json_path, "png": {"gam": png}, "window_ms": [float(tw[0]), float(tw[-1])],
          "df_khz": float(freq[1]), "samples": samples})
    return 0


if __name__ == "__main__":
    sys.exit(main())
