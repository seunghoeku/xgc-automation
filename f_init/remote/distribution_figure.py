#!/usr/bin/env python3
"""Login-node step: the paper's distribution figure (Fig. 1) from f_init files.

For one mesh node (default: the outboard-midplane node nearest ``--psi``) the
relative deviation of the transferred distribution from the local Maxwellian,

    (f - f_M) / f_M = f_g / f_M   [%],

is drawn over the velocity grid for the unsmoothed f_init file and for the
smoothed one, with a diverging colour map centred at zero, one symmetric
colour range for both panels, a labelled colour bar, and the node's
``psi_N`` in the title (referee requests: colour bar, units, contrast,
location). Cells where f_M is below ``--mask-fraction`` of its maximum at the
node are left white: the ratio is meaningless there.

Last stdout line: ``FINIT_JSON {...}``.
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
    parser.add_argument("--run-dir", required=True, help="stage A run (mesh)")
    parser.add_argument("--no-smooth", required=True, help="unsmoothed f_init file")
    parser.add_argument("--smooth", required=True, help="smoothed f_init file")
    parser.add_argument("--smooth-label", default="Smoothing")
    parser.add_argument("--node", type=int, default=None, help="mesh node; default from --psi")
    parser.add_argument("--psi", type=float, default=0.45, help="psi_N of the outboard-midplane node")
    parser.add_argument("--clip-percent", type=float, default=None,
                        help="symmetric colour range [%%]; default 98th percentile of |panel (a)|")
    parser.add_argument("--mask-fraction", type=float, default=1e-2)
    parser.add_argument("--normalize", choices=["local", "peak"], default="local",
                        help="divide f - f_M by the local f_M (per velocity cell) or by the node's "
                             "peak f_M (shows the tails without the ratio blowing up)")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    run_dir = os.path.abspath(args.run_dir)
    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)
    for path in (args.no_smooth, args.smooth):
        if not os.path.exists(path):
            emit({"error": "missing", "paths": [path]})
            return 2

    x = xgc_reader.xgc1(path=run_dir)   # chdir
    x.load_unitsm()
    x.setup_mesh()
    psi_n = np.asarray(x.mesh.psi) / x.psix
    r_ax = float(getattr(x, "eq_axis_r", np.mean(x.mesh.r)))
    if args.node is None:
        mid = np.where((np.abs(x.mesh.z) < 0.01) & (x.mesh.r > r_ax))[0]
        node = int(mid[np.argmin(np.abs(psi_n[mid] - args.psi))])
    else:
        node = int(args.node)
    where = {"node": node, "R_m": float(x.mesh.r[node]), "Z_m": float(x.mesh.z[node]),
             "psi_n": float(psi_n[node])}

    panels = []
    for path, label in ((args.no_smooth, "No smoothing"), (args.smooth, args.smooth_label)):
        dist = xd.XGCDistribution.from_xgc_input(os.path.abspath(path))
        fm = dist.get_maxwellian()[node]          # (nvperp, nvpara)
        fg = np.asarray(dist.f_g)[node]
        rel = np.full_like(fg, np.nan)
        if args.normalize == "local":
            keep = fm > args.mask_fraction * fm.max()
            rel[keep] = 100.0 * fg[keep] / fm[keep]
        else:
            rel[:] = 100.0 * fg / fm.max()
        panels.append({"label": label, "file": os.path.abspath(path), "rel": rel,
                       "vpara": np.asarray(dist.vgrid.vpara), "vperp": np.asarray(dist.vgrid.vperp),
                       "temp_ev": float(np.asarray(dist.temp_ev)[node]),
                       "den": float(np.asarray(dist.den)[node]),
                       "rms_percent": float(np.sqrt(np.nanmean(rel ** 2))),
                       "max_percent": float(np.nanmax(np.abs(rel)))})

    clip = args.clip_percent or float(np.nanpercentile(np.abs(panels[0]["rel"]), 98))
    font, figsize, dpi = (28, (13, 10), 180) if args.paper else (16, (8, 6.5), 150)
    plt.rcParams.update({"font.size": font})
    levels = np.linspace(-clip, clip, 61)
    outputs = {}
    for k, p in enumerate(panels):
        fig, ax = plt.subplots(figsize=figsize)
        z = np.clip(np.ma.masked_invalid(p["rel"]), -clip, clip)
        cf = ax.contourf(p["vpara"], p["vperp"], z, levels=levels, cmap="RdBu_r", extend="both")
        ax.set_xlabel("$u_\\parallel / v_{th}$")
        ax.set_ylabel("$v_\\perp / v_{th}$")
        ax.set_title(f"({'ab'[k]}) {p['label']}")
        cb = fig.colorbar(cf, ax=ax)
        cb.set_label("$(f - f_M)/f_M$ (%)" if args.normalize == "local" else "$(f - f_M)/\\max f_M$ (%)")
        ax.text(0.02, 0.97, f"$\\psi_N = {where['psi_n']:.2f}$, outboard midplane",
                transform=ax.transAxes, va="top", fontsize=int(font * 0.9),
                bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"})
        fig.tight_layout()
        path = f"{out_prefix}{'' if args.normalize == 'local' else '_peak'}_{'ab'[k]}.png"
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
        outputs["ab"[k]] = path

    summary = [{k: v for k, v in p.items() if k not in ("rel", "vpara", "vperp")} for p in panels]
    json_path = f"{out_prefix}{'' if args.normalize == 'local' else '_peak'}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"where": where, "clip_percent": clip, "mask_fraction": args.mask_fraction,
                   "normalize": args.normalize,
                   "vth_definition": "sqrt(fg_temp_ev * e / m), the f_g grid normalisation",
                   "panels": summary}, fh, indent=1)
    emit({"where": where, "clip_percent": clip, "panels": summary, "json": json_path, "png": outputs})
    return 0


if __name__ == "__main__":
    sys.exit(main())
