#!/usr/bin/env python3
"""Login-node step: the relaxed radial electric field of the stage A run.

E_r(psi_N) is taken from the flux-surface-averaged potential of
``xgc.oneddiag.bp``: E_r = -(d<phi>/dpsi) |grad psi|, with |grad psi| the
flux-surface average XGC writes (``i_grad_psi_sqr_1d``). Drawn at the f0
step the f_init came from and at the last step, together with the E_r the
initial profiles would give if the plasma were force-free (zero). Values are
also written to JSON. Last stdout line: ``FINIT_JSON {...}``.
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
    parser.add_argument("--f0-step", type=int, default=None, help="step the f_init came from")
    parser.add_argument("--steps", default=None,
                        help="comma-separated steps to draw instead of f0-step/last; 'first' and 'last' allowed")
    parser.add_argument("--labels", default=None, help="'|'-separated legend suffixes for --steps")
    parser.add_argument("--smooth-psi", type=float, default=0.0,
                        help="Gaussian smoothing width in psi_N applied to <E_r> (0: none)")
    parser.add_argument("--psi-min", type=float, default=0.0)
    parser.add_argument("--psi-max", type=float, default=0.9,
                        help="right edge of the plot; the outer boundary layer is excluded")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--title", default="Relaxed radial electric field, axisymmetric CBC")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from xgc_reader import base as xgc_reader

    run_dir = os.path.abspath(args.run_dir)
    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)

    x = xgc_reader.xgc1(path=run_dir)
    x.load_unitsm()
    x.load_oned()
    od = x.od
    steps = np.asarray(od.step)
    # restarts repeat steps; keep the last occurrence of each
    _, first_rev = np.unique(steps[::-1], return_index=True)
    keep = np.sort(len(steps) - 1 - first_rev)
    steps = steps[keep]
    # <phi>_00 lives on the finer psi00 grid (normalised psi); |grad psi| on
    # the 1-D diagnostic grid. Differentiate on psi00 in Wb/rad and bring
    # <|grad psi|> over by interpolation.
    psi_mks = np.asarray(od.psi00)                      # psi00 is in Wb/rad (sml_00 grid)
    psi = psi_mks / float(x.psix)                        # normalised
    pot = np.asarray(od.pot00_1d)[keep]                 # (nt, npsi00) [V]
    dphi = np.gradient(pot, psi_mks, axis=1)
    g2 = np.asarray(od.grad_psi_sqr)
    g2 = g2[keep] if g2.ndim == 2 else np.broadcast_to(g2, (pot.shape[0], g2.size))
    od_psi = np.asarray(od.psi)
    od_psi = od_psi[0] if od_psi.ndim == 2 else od_psi
    gpsi = np.vstack([np.interp(psi, od_psi, np.sqrt(np.abs(row))) for row in g2])
    er = -dphi * gpsi                                    # V/m, (nt, npsi00)
    if args.smooth_psi > 0:
        from scipy.ndimage import gaussian_filter1d
        dpsi = float(np.median(np.diff(psi)))
        er = gaussian_filter1d(er, args.smooth_psi / dpsi, axis=1, mode="nearest")

    t = np.asarray(od.time)[keep] * 1e3
    if args.steps:
        idx = []
        for tok in args.steps.split(","):
            tok = tok.strip()
            if tok == "first":
                idx.append(0)
            elif tok == "last":
                idx.append(len(steps) - 1)
            else:
                idx.append(int(np.argmin(np.abs(steps - int(tok)))))
        labels = args.labels.split("|") if args.labels else [""] * len(idx)
    else:
        if args.f0_step is None:
            parser.error("--f0-step or --steps is required")
        hit = np.where(steps == args.f0_step)[0]
        i0 = int(hit[0]) if len(hit) else int(len(steps) - 1)
        idx, labels = [i0], ["f_init snapshot"]
        if len(steps) - 1 != i0:
            idx.append(len(steps) - 1)
            labels.append("end of run")

    font, figsize, dpi, lw = (28, (13, 10), 180, 3.0) if args.paper else (16, (9, 6.5), 150, 2.2)
    plt.rcParams.update({"font.size": font})
    fig, ax = plt.subplots(figsize=figsize)
    for k, (i, lab) in enumerate(zip(idx, labels)):
        ax.plot(psi, er[i] * 1e-3, ["-", "--", ":", "-."][k % 4], lw=lw,
                label=f"t = {t[i]:.2f} ms" + (f" ({lab})" if lab else ""))
    ax.axhline(0.0, color="0.5", lw=1)
    ax.set_xlabel("$\\psi_N$")
    ax.set_ylabel("$\\langle E_r \\rangle$ (kV/m)")
    ax.set_title(args.title)
    ax.set_xlim(args.psi_min, args.psi_max)
    shown = (psi >= args.psi_min) & (psi <= args.psi_max)
    lo = min(er[i][shown].min() for i in idx) * 1e-3
    hi = max(er[i][shown].max() for i in idx) * 1e-3
    pad = 0.06 * (hi - lo)
    ax.set_ylim(lo - pad, hi + pad)
    ax.legend(fontsize=int(font * 0.8))
    fig.tight_layout()
    png = f"{out_prefix}.png"
    fig.savefig(png, dpi=dpi)
    plt.close(fig)

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"psi_n": psi.tolist(), "time_ms": [float(t[i]) for i in idx],
                   "steps": [int(steps[i]) for i in idx],
                   "er_v_per_m": [er[i].tolist() for i in idx], "smooth_psi": args.smooth_psi,
                   "definition": "E_r = -(d<phi>_00/dpsi) * sqrt(<|grad psi|^2>); pot00_1d on psi00, grad_psi_sqr interpolated"}, fh)
    shown_er = er[idx[0]][shown]
    emit({"json": json_path, "png": {"er": png}, "steps": [int(steps[i]) for i in idx],
          "time_ms": [float(t[i]) for i in idx], "step_range": [int(steps[0]), int(steps[-1])],
          "er_range_kv_per_m": [float(shown_er.min() * 1e-3), float(shown_er.max() * 1e-3)]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
