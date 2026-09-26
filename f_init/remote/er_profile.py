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
    parser.add_argument("--f0-step", type=int, required=True, help="step the f_init came from")
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
    x.setup_mesh()
    x.load_oned()
    od = x.od
    steps = np.asarray(od.step)
    # <phi>_00 lives on the finer psi00 grid (normalised psi); |grad psi| on
    # the 1-D diagnostic grid. Differentiate on psi00 in Wb/rad and bring
    # <|grad psi|> over by interpolation.
    psi_mks = np.asarray(od.psi00)                      # psi00 is in Wb/rad (sml_00 grid)
    psi = psi_mks / float(x.psix)                        # normalised
    pot = np.asarray(od.pot00_1d)                       # (nt, npsi00) [V]
    dphi = np.gradient(pot, psi_mks, axis=1)
    g2 = np.asarray(od.grad_psi_sqr)
    if g2.ndim == 1:
        g2 = np.broadcast_to(g2, (pot.shape[0], g2.size))
    gpsi = np.vstack([np.interp(psi, np.asarray(od.psi), np.sqrt(np.abs(row))) for row in g2])
    er = -dphi * gpsi                                    # V/m, (nt, npsi00)

    hit = np.where(steps == args.f0_step)[0]
    i0 = int(hit[0]) if len(hit) else int(len(steps) - 1)
    i1 = int(len(steps) - 1)
    t = np.asarray(od.time) * 1e3

    font, figsize, dpi, lw = (28, (13, 10), 180, 3.0) if args.paper else (16, (9, 6.5), 150, 2.2)
    plt.rcParams.update({"font.size": font})
    fig, ax = plt.subplots(figsize=figsize)
    ax.plot(psi, er[i0] * 1e-3, "-", lw=lw, label=f"t = {t[i0]:.2f} ms (f_init snapshot)")
    if i1 != i0:
        ax.plot(psi, er[i1] * 1e-3, "--", lw=lw, label=f"t = {t[i1]:.2f} ms (end of run)")
    ax.axhline(0.0, color="0.5", lw=1)
    ax.set_xlabel("$\\psi_N$")
    ax.set_ylabel("$\\langle E_r \\rangle$ (kV/m)")
    ax.set_title(args.title)
    ax.set_xlim(0.0, args.psi_max)
    shown = psi <= args.psi_max
    lo = min(er[i0][shown].min(), er[i1][shown].min()) * 1e-3
    hi = max(er[i0][shown].max(), er[i1][shown].max()) * 1e-3
    pad = 0.06 * (hi - lo)
    ax.set_ylim(lo - pad, hi + pad)
    ax.legend(fontsize=int(font * 0.8))
    fig.tight_layout()
    png = f"{out_prefix}.png"
    fig.savefig(png, dpi=dpi)
    plt.close(fig)

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"psi_n": psi.tolist(), "time_ms": [float(t[i0]), float(t[i1])],
                   "steps": [int(steps[i0]), int(steps[i1])],
                   "er_v_per_m": [er[i0].tolist(), er[i1].tolist()],
                   "definition": "E_r = -(d<phi>_00/dpsi) * sqrt(<|grad psi|^2>); pot00_1d on psi00, grad_psi_sqr interpolated"}, fh)
    emit({"json": json_path, "png": {"er": png}, "steps": [int(steps[i0]), int(steps[i1])],
          "time_ms": [float(t[i0]), float(t[i1])],
          "er_range_kv_per_m": [float(np.min(er[i0]) * 1e-3), float(np.max(er[i0]) * 1e-3)]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
