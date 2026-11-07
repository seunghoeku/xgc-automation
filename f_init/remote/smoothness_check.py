#!/usr/bin/env python3
"""Login-node step: how smooth is an f_init file?

For each f_init file the non-Maxwellian part is normalised per node by the
peak of the local Maxwellian, ``g = f_g / max_v f_M``, and three roughness
ratios are measured on every closed flux surface (``mesh.region == 1``):

* ``pol``  - rms of the node-to-node difference of g along the surface
  (periodic, surf_idx order) divided by the rms of g on that surface;
* ``vpar`` / ``vperp`` - the same for neighbouring velocity cells, over the
  cells where f_M is above ``--mask-fraction`` of its node peak;
* ``dn_pol`` - the same poloidal ratio for the density moment of f_g,
  ``dn/n = sum(f_g vperp) / sum(f_M vperp)`` per node (what the turbulence
  run actually sees first).

A field that is white noise from one point to the next gives a ratio of
sqrt(2) = 1.41; a field resolved by the grid gives a ratio well below one.
For a smoothed file the script also reports how much of the unsmoothed g
was removed, ``rms(g_raw - g_smooth) / rms(g_raw)``, per surface.

Results are binned by surface psi_N; JSON and one PNG per case are written
to ``--out-prefix``. Last stdout line: ``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _xgc1(module, path):
    """Open a run with the legacy XGC_reader backend.

    XGC_reader's XGC_analysis branch (bca9f07 and later, which carries
    set_sol_maxwellian) defaults to ``backend="analysis"``, which needs the
    separate XGC-Analysis package; these scripts use only the legacy reader.
    Older checkouts have no ``backend`` argument at all.
    """
    import inspect
    if "backend" in inspect.signature(module.xgc1.__init__).parameters:
        return module.xgc1(path=path, backend="legacy")
    return module.xgc1(path=path)


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="stage A run (mesh)")
    parser.add_argument("--files", required=True, nargs="+", help="f_init files, first = reference (unsmoothed)")
    parser.add_argument("--labels", nargs="+", default=None)
    parser.add_argument("--mask-fraction", type=float, default=1e-2)
    parser.add_argument("--psi-min", type=float, default=0.0)
    parser.add_argument("--title", default="")
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
    files = [os.path.abspath(f) for f in args.files]
    missing = [f for f in files if not os.path.exists(f)]
    if missing:
        emit({"error": "missing", "paths": missing})
        return 2
    labels = args.labels or [os.path.basename(f) for f in files]

    x = _xgc1(xgc_reader, run_dir)   # chdir
    x.load_unitsm()
    x.setup_mesh()
    psi_n = np.asarray(x.mesh.psi) / x.psix
    region = np.asarray(x.mesh.region)
    surf_len = np.asarray(x.mesh.surf_len)
    surf_idx = np.asarray(x.mesh.surf_idx)

    # Closed surfaces: every node of the row is region 1 and the row is long
    # enough to be a loop.
    surfaces = []
    for i in range(len(surf_len)):
        idx = surf_idx[i, : surf_len[i]] - 1
        if len(idx) < 8 or not np.all(region[idx] == 1):
            continue
        p = float(np.mean(psi_n[idx]))
        if p < args.psi_min:
            continue
        surfaces.append((p, idx))
    surfaces.sort(key=lambda s: s[0])

    def rough(a: np.ndarray, axis: int, periodic: bool, mask: np.ndarray | None = None) -> float:
        d = (np.roll(a, -1, axis=axis) - a) if periodic else np.diff(a, axis=axis)
        if mask is not None:
            m = (np.roll(mask, -1, axis=axis) & mask) if periodic else (
                np.take(mask, range(1, mask.shape[axis]), axis=axis)
                & np.take(mask, range(0, mask.shape[axis] - 1), axis=axis))
            d = d[m]
            ref = a[mask]
        else:
            ref = a
        s = float(np.sqrt(np.mean(ref ** 2)))
        return float(np.sqrt(np.mean(d ** 2)) / s) if s > 0 else float("nan")

    per_file = []
    g_ref = None
    for path, label in zip(files, labels):
        dist = xd.XGCDistribution.from_xgc_input(path)
        fm = np.asarray(dist.get_maxwellian())            # (n, nvperp, nvpara)
        fg = np.asarray(dist.f_g)
        peak = fm.reshape(fm.shape[0], -1).max(axis=1)[:, None, None]
        g = fg / peak
        mask = fm > args.mask_fraction * peak
        vperp = np.asarray(dist.vgrid.vperp)[None, :, None]
        dn = (fg * vperp).sum(axis=(1, 2)) / (fm * vperp).sum(axis=(1, 2))
        rows = []
        for p, idx in surfaces:
            gs = g[idx]
            ms = mask[idx]
            row = {
                "psi_n": p,
                "nodes": int(len(idx)),
                "g_rms": float(np.sqrt(np.mean(gs[ms] ** 2))),
                "pol": rough(np.where(ms, gs, 0.0), 0, True),
                "vperp": rough(gs, 1, False, ms),
                "vpar": rough(gs, 2, False, ms),
                "dn_rms": float(np.sqrt(np.mean(dn[idx] ** 2))),
                "dn_pol": rough(dn[idx], 0, True),
                "dn_pol_std": float(np.std(dn[idx])),
            }
            if g_ref is not None:
                gr = g_ref[idx]
                sref = float(np.sqrt(np.mean(gr[ms] ** 2)))
                row["removed"] = float(np.sqrt(np.mean((gr - gs)[ms] ** 2)) / sref) if sref > 0 else float("nan")
            rows.append(row)
        if g_ref is None:
            g_ref = g
        per_file.append({"label": label, "file": path, "surfaces": rows})

    def summary(rows: list[dict], lo: float, hi: float) -> dict:
        sel = [r for r in rows if lo <= r["psi_n"] < hi]
        if not sel:
            return {}
        keys = [k for k in sel[0] if k not in ("psi_n", "nodes")]
        return {k: float(np.median([r[k] for r in sel])) for k in keys} | {"surfaces": len(sel)}

    bands = [(0.0, 0.5), (0.5, 0.9), (0.9, 0.95), (0.95, 1.0)]
    table = {}
    for pf in per_file:
        table[pf["label"]] = {f"{lo:.2f}-{hi:.2f}": summary(pf["surfaces"], lo, hi) for lo, hi in bands}

    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5), sharex=True)
    styles = ["-", "--", ":", "-."]
    for k, pf in enumerate(per_file):
        ps = [r["psi_n"] for r in pf["surfaces"]]
        for ax, key, name in ((axes[0, 0], "pol", "poloidal  $g$"), (axes[0, 1], "vpar", "$v_\\parallel$  $g$"),
                              (axes[1, 0], "vperp", "$v_\\perp$  $g$"), (axes[1, 1], "dn_pol", "poloidal  $\\delta n/n$")):
            ax.plot(ps, [r[key] for r in pf["surfaces"]], styles[k % 4], label=pf["label"])
            ax.set_title(f"roughness: {name}")
    for ax in axes.flat:
        ax.axhline(np.sqrt(2), color="0.5", lw=0.8)
        ax.text(0.01, np.sqrt(2), " white noise", color="0.4", va="bottom", fontsize=9,
                transform=ax.get_yaxis_transform())
        ax.set_ylim(0, 1.6)
        ax.grid(alpha=0.3)
    for ax in axes[1]:
        ax.set_xlabel("$\\psi_N$ (closed surfaces)")
    axes[0, 0].legend(fontsize=9)
    fig.suptitle(args.title or "f_init roughness: rms(neighbour difference) / rms")
    fig.tight_layout()
    png = f"{out_prefix}.png"
    fig.savefig(png, dpi=140)
    plt.close(fig)

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"definition": __doc__, "mask_fraction": args.mask_fraction,
                   "bands_median": table, "files": per_file}, fh, indent=1)
    emit({"json": json_path, "png": {"roughness": png}, "bands_median": table,
          "surfaces": len(surfaces)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
