#!/usr/bin/env python3
"""Login-node step: canonical-Maxwellian ``f_init`` matched to an XGCa f0 snapshot.

The target density and temperature are the flux-surface averages of the
moments of ``xgc.f0.<step>.bp``; ``set_canonical_maxwellian_iterative`` then
finds F(psi_c, E) whose flux-averaged moments reproduce them. The file is
written with the flux-averaged Maxwellian removed, like every other f_init, so
``f_g`` carries the canonical shape and its poloidal variation. The last
stdout line is ``FINIT_JSON {...}`` for the local driver.

Needs the XGC_reader fix of 2026-09-23 (B_phi sign of the orbit-width
correction, v_perp=0 cell); older checkouts are refused.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True, help="XGC_reader checkout")
    parser.add_argument("--run-dir", required=True, help="XGCa run directory")
    parser.add_argument("--step", type=int, required=True, help="f0 step")
    parser.add_argument("--out-name", default="xgc.f_init_canonical.bp")
    parser.add_argument("--var-string", default="i_f")
    parser.add_argument("--mass-au", type=float, default=2.0)
    parser.add_argument("--charge-num", type=float, default=1.0)
    parser.add_argument("--has-electron", action="store_true")
    parser.add_argument("--no-correction", action="store_true",
                        help="plain psi_c, without the orbit-width correction")
    parser.add_argument("--tol", type=float, default=1e-3)
    parser.add_argument("--max-iter", type=int, default=60)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    out_path = os.path.join(run_dir, args.out_name)
    if os.path.exists(out_path) and not args.overwrite:
        emit({"error": "exists", "paths": [out_path]})
        return 2
    f0_file = f"xgc.f0.{args.step:05d}.bp"
    if not os.path.exists(os.path.join(run_dir, f0_file)):
        emit({"error": "missing", "paths": [os.path.join(run_dir, f0_file)]})
        return 2

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    if "bphi_sign" not in inspect.getsource(xd.XGCDistribution.canonical_maxwellian):
        emit({"error": "XGC_reader lacks the canonical_maxwellian B_phi-sign fix",
              "paths": [args.xgc_reader]})
        return 2

    x = xgc_reader.xgc1(path=run_dir)   # note: chdir into run_dir
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()
    x.load_bfield()
    x.load_oned()

    dist = xd.XGCDistribution.from_xgc_output(
        f0_file, time_step=0, mass_au=args.mass_au, charge_num=args.charge_num,
        var_string=args.var_string, dir=run_dir, has_electron=args.has_electron,
        use_initial_moments=True,
    )
    # target: flux-surface averages of the snapshot's own moments
    den_l, temp_l, _flow_l = xd.get_local_moments(dist, x)
    psi_s = np.asarray(x.mesh.psi_surf, dtype=float)
    den_s = np.asarray(x.fsa_simple(den_l), dtype=float)
    temp_s = np.asarray(x.fsa_simple(temp_l), dtype=float)
    keep = np.isfinite(den_s) & np.isfinite(temp_s) & (den_s > 0) & (temp_s > 0)
    order = np.argsort(psi_s[keep])
    psi_s, den_s, temp_s = psi_s[keep][order], den_s[keep][order], temp_s[keep][order]

    dist.set_canonical_maxwellian_iterative(
        x, psi_s, den_s, psi_s, temp_s, correction=not args.no_correction,
        tol=args.tol, max_iter=args.max_iter,
    )
    # achieved flux-averaged moments against the target
    den_a = np.interp(psi_s, x.mesh.psi_surf, x.fsa_simple(dist.den))
    temp_a = np.interp(psi_s, x.mesh.psi_surf, x.fsa_simple(dist.temp_ev))
    den_rel = (den_a - den_s) / den_s
    temp_rel = (temp_a - temp_s) / temp_s
    fcm_min = float(np.min(dist.f))

    dist.save(out_path)
    emit({
        "f0_step": args.step,
        "f0_file": os.path.join(run_dir, f0_file),
        "output": out_path,
        "correction": not args.no_correction,
        "nnodes": int(dist.nnodes),
        "nvperp": int(dist.vgrid.nvperp),
        "nvpara": int(dist.vgrid.nvpara),
        "private_region_nodes": int(np.sum(x.mesh.region > 2)),
        "fsa_den_rel_err": {"rms": float(np.sqrt(np.mean(den_rel**2))),
                            "abs_max": float(np.max(np.abs(den_rel)))},
        "fsa_temp_rel_err": {"rms": float(np.sqrt(np.mean(temp_rel**2))),
                             "abs_max": float(np.max(np.abs(temp_rel)))},
        "f_full_min": fcm_min,
        "den_range": [float(np.min(dist.den)), float(np.max(dist.den))],
        "temp_ev_range": [float(np.min(dist.temp_ev)), float(np.max(dist.temp_ev))],
        "f_g_stats": {"abs_max": float(np.max(np.abs(dist.f_g))),
                      "rms": float(np.sqrt(np.mean(dist.f_g**2)))},
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
