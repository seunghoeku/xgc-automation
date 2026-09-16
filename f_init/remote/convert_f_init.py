#!/usr/bin/env python3
"""Login-node step: interpolate an f_init file onto the turbulence mesh (case 2).

Reproduces the "make distribution" cell of cbc5_f_init_turb_prepare.ipynb:
``xd.convert_distribution(dist, x_source, x_target)``. The source reader is
the XGCa run that produced the f_init (its mesh triangulation); the target
reader is any directory holding ``xgc.mesh.bp``/``xgc.units.bp``/``xgc.bfield.bp``
written by XGC on the turbulence mesh. Only the target's mesh is used - the
velocity grid travels with the distribution - so the target run's f0 grid may
differ, but the turbulence input that consumes the result must use the f_init's
velocity grid (f0_nmu/f0_nvp/f0_smu_max/f0_vp_max).

Target nodes outside the source mesh take the nearest source node's value
(convert_distribution does that itself). The last stdout line is
``FINIT_JSON {...}`` for the local driver.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="source XGCa run (mesh of the f_init)")
    parser.add_argument("--target-dir", required=True, help="directory with xgc.mesh.bp of the turbulence mesh")
    parser.add_argument("--finit", required=True, help="f_init file to convert")
    parser.add_argument("--out", required=True, help="converted f_init path")
    parser.add_argument("--png-out", default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    target_dir = os.path.abspath(args.target_dir)
    finit = os.path.abspath(args.finit)
    out = os.path.abspath(args.out)
    for path in (finit, os.path.join(target_dir, "xgc.mesh.bp"), os.path.join(target_dir, "xgc.units.bp")):
        if not os.path.exists(path):
            emit({"error": "missing", "paths": [path]})
            return 2
    if os.path.exists(out) and not args.overwrite:
        emit({"error": "exists", "paths": [out]})
        return 3

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    t0 = time.time()
    x_src = xgc_reader.xgc1(path=run_dir)
    x_src.load_unitsm(); x_src.setup_mesh(); x_src.setup_f0mesh()
    x_dst = xgc_reader.xgc1(path=target_dir)
    x_dst.load_unitsm(); x_dst.setup_mesh()
    try:
        x_dst.setup_f0mesh()
    except Exception as exc:  # noqa: BLE001 - the target f0 grid is not needed
        print(f"note: target f0 mesh not loaded ({exc}); not needed for conversion")

    dist = xd.XGCDistribution.from_xgc_input(finit)
    # Nodes of the target outside the source triangulation (filled by nearest node).
    tf = x_src.mesh.triobj.get_trifinder()
    outside = tf(x_dst.mesh.r, x_dst.mesh.z) == -1
    src_psin = np.asarray(x_src.mesh.psi) / x_src.unit_dic["eq_x_psi"]
    dst_psin = np.asarray(x_dst.mesh.psi) / x_dst.unit_dic["eq_x_psi"]

    # Interpolate only; the Maxwellian moments on the target are recomputed
    # below. convert_distribution's own update_maxwellian_moments would call
    # xgc_reader.flux_surface_average, whose psi interpolation is not monotonic
    # when private-flux surfaces follow the SOL surfaces in psi_surf, and hands
    # far-SOL nodes a private-region density/temperature (seen on the Jul24
    # ASDEX mesh: a constant 4.5e18 m^-3 / 170 eV band beyond psi_n 1.0).
    dist_turb = xd.convert_distribution(dist, x_src, x_dst, update_moments=False)
    den_n, temp_n, flow_n = dist_turb.update_maxwellian_moments(x_dst, do_flux_average=False, no_update=True)

    def fsa_scatter(var):
        # volume-weighted surface mean scattered through surf_idx/surf_len;
        # nodes on no stored surface (wall) keep their node-wise value.
        surf = x_dst.fsa_simple(var)
        out = np.array(var, dtype=float, copy=True)
        for i in range(surf.size):
            length = int(x_dst.mesh.surf_len[i])
            if length > 0:
                out[np.asarray(x_dst.mesh.surf_idx[i, :length]) - 1] = surf[i]
        return out

    r_dst = np.asarray(x_dst.mesh.r)
    dist_turb.den = fsa_scatter(den_n)
    dist_turb.temp_ev = fsa_scatter(temp_n)
    dist_turb.flow = fsa_scatter(flow_n / r_dst) * r_dst   # u/R is the flux function
    dist_turb.remove_maxwellian()
    den_src = np.asarray(dist.den); den_dst = np.asarray(dist_turb.den)
    n_no_surface = int(np.sum(~np.isin(np.arange(x_dst.mesh.nnodes),
                                        np.concatenate([np.asarray(x_dst.mesh.surf_idx[i, :int(x_dst.mesh.surf_len[i])]) - 1
                                                        for i in range(np.size(x_dst.mesh.surf_len)) if int(x_dst.mesh.surf_len[i]) > 0]))))
    fg = np.asarray(dist_turb.f_g if getattr(dist_turb, 'f_g', None) is not None else dist_turb.f)  # Maxwellian removed -> f_g
    nan_nodes = int(np.sum(~np.isfinite(fg).all(axis=(1, 2))))
    dist_turb.save(out)

    def rng(a):
        a = np.asarray(a); a = a[np.isfinite(a)]
        return [float(a.min()), float(a.max())] if a.size else None

    payload = {
        "run_dir": run_dir, "target_dir": target_dir, "finit": finit, "output": out,
        "nnodes_source": int(x_src.mesh.nnodes), "nnodes_target": int(x_dst.mesh.nnodes),
        "vgrid": {"nvperp": int(dist.vgrid.nvperp), "nvpara_data": int(dist.vgrid.nvpdata)},
        "target_nodes_outside_source_mesh": int(outside.sum()),
        "target_psin_range": rng(dst_psin), "source_psin_range": rng(src_psin),
        "outside_nodes_psin_range": rng(dst_psin[outside]) if outside.any() else None,
        "density_range_source": rng(den_src), "density_range_target": rng(den_dst),
        "fg_stats_target": {"abs_max": float(np.nanmax(np.abs(fg))), "rms": float(np.sqrt(np.nanmean(fg**2)))},
        "nan_nodes_target": nan_nodes,
        "target_nodes_on_no_surface_nodewise_moments": n_no_surface,
        "moments": "volume-weighted flux-surface average via surf_idx/surf_len; node-wise where no surface",
        "seconds": round(time.time() - t0, 1),
    }

    if args.png_out:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, axes = plt.subplots(1, 3, figsize=(16, 5))
            axes[0].plot(src_psin, den_src, ".", ms=2, alpha=.4, label=f"source ({x_src.mesh.nnodes} nodes)")
            axes[0].plot(dst_psin, den_dst, ".", ms=1, alpha=.3, label=f"target ({x_dst.mesh.nnodes} nodes)")
            axes[0].set_xlabel("psi_n"); axes[0].set_ylabel("f_init density [m^-3]"); axes[0].legend(); axes[0].set_title("density moment")
            axes[1].plot(src_psin, np.asarray(dist.temp_ev), ".", ms=2, alpha=.4, label="source")
            axes[1].plot(dst_psin, np.asarray(dist_turb.temp_ev), ".", ms=1, alpha=.3, label="target")
            axes[1].set_xlabel("psi_n"); axes[1].set_ylabel("T [eV]"); axes[1].legend(); axes[1].set_title("temperature moment")
            axes[2].triplot(x_src.mesh.triobj, lw=0.2, color="0.6")
            axes[2].plot(x_dst.mesh.r[outside], x_dst.mesh.z[outside], "r.", ms=1, label=f"{int(outside.sum())} target nodes outside source")
            axes[2].set_aspect("equal"); axes[2].legend(loc="upper right", fontsize=8); axes[2].set_title("source mesh, uncovered target nodes")
            fig.suptitle(os.path.basename(out)); fig.tight_layout(); fig.savefig(args.png_out, dpi=100)
            payload["png"] = os.path.abspath(args.png_out)
        except Exception as exc:  # noqa: BLE001
            payload["png_error"] = str(exc)

    emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
