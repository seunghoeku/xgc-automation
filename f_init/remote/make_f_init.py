#!/usr/bin/env python3
"""Login-node step: XGCa f0 snapshot -> XGC ``f_init`` distribution files.

Reproduces the "make distribution" cell of the f_init notebook with one
smoothing setting. Writes ``xgc.f_init_no_smooth.bp`` and, when either
smoothing width is positive, ``xgc.f_init_smooth_<sigma>_<width>.bp``. The
last stdout line is ``FINIT_JSON {...}`` for the local driver.

Runs with the analysis Python (adios2, numpy, scipy) and a checkout of
XGC_reader. File-access dominant and bounded (minutes), so a login node is the
right place for it.
"""

from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import re
import sys


def last_f0_step(run_dir: str) -> int:
    steps = []
    for path in glob.glob(os.path.join(run_dir, "xgc.f0.[0-9]*.bp")):
        match = re.search(r"xgc\.f0\.(\d+)\.bp$", path)
        if match:
            steps.append(int(match.group(1)))
    if not steps:
        raise SystemExit(f"no xgc.f0.<step>.bp under {run_dir}")
    return max(steps)


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True, help="XGC_reader checkout")
    parser.add_argument("--run-dir", required=True, help="XGCa run directory")
    parser.add_argument("--out-dir", default=None, help="default: run dir")
    parser.add_argument("--step", type=int, default=None, help="f0 step; default last")
    parser.add_argument("--var-string", default="i_f")
    parser.add_argument("--mass-au", type=float, default=2.0)
    parser.add_argument("--charge-num", type=float, default=1.0)
    parser.add_argument("--has-electron", action="store_true")
    parser.add_argument("--sigma-theta", type=float, default=0.0)
    parser.add_argument("--v-width", type=float, default=0.0)
    parser.add_argument("--no-smooth-name", default="xgc.f_init_no_smooth.bp")
    parser.add_argument("--smooth-name", default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    out_dir = os.path.abspath(args.out_dir or run_dir)
    os.makedirs(out_dir, exist_ok=True)

    do_smooth = args.sigma_theta > 0 or args.v_width > 0
    targets = [os.path.join(out_dir, args.no_smooth_name)]
    if do_smooth:
        if not args.smooth_name:
            raise SystemExit("--smooth-name is required when smoothing is requested")
        targets.append(os.path.join(out_dir, args.smooth_name))
    clashes = [t for t in targets if os.path.exists(t)]
    if clashes and not args.overwrite:
        emit({"error": "exists", "paths": clashes})
        return 2

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    step = args.step if args.step is not None else last_f0_step(run_dir)
    f0_file = f"xgc.f0.{step:05d}.bp"
    if not os.path.exists(os.path.join(run_dir, f0_file)):
        emit({"error": "missing", "paths": [os.path.join(run_dir, f0_file)]})
        return 2

    x = xgc_reader.xgc1(path=run_dir)   # note: chdir into run_dir
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()

    dist = xd.XGCDistribution.from_xgc_output(
        f0_file,
        time_step=0,
        mass_au=args.mass_au,
        charge_num=args.charge_num,
        var_string=args.var_string,
        dir=run_dir,
        has_electron=args.has_electron,
        use_initial_moments=True,
    )
    dist.remove_maxwellian()

    outputs = []
    dist.save(targets[0])
    outputs.append({"kind": "no_smooth", "path": targets[0]})

    fg_stats = {
        "no_smooth": {
            "abs_max": float(np.max(np.abs(dist.f_g))),
            "rms": float(np.sqrt(np.mean(dist.f_g**2))),
        }
    }
    if do_smooth:
        smooth = copy.deepcopy(dist)
        if args.sigma_theta > 0:
            xd.poloidal_smooth_f_init(smooth, x, sigma_theta=args.sigma_theta)
        if args.v_width > 0:
            xd.vspace_smooth(smooth, width=args.v_width)
        smooth.save(targets[1])
        outputs.append({"kind": "smooth", "path": targets[1]})
        fg_stats["smooth"] = {
            "abs_max": float(np.max(np.abs(smooth.f_g))),
            "rms": float(np.sqrt(np.mean(smooth.f_g**2))),
        }

    emit({
        "f0_step": step,
        "f0_file": os.path.join(run_dir, f0_file),
        "nnodes": int(dist.nnodes),
        "nvperp": int(dist.vgrid.nvperp),
        "nvpara": int(dist.vgrid.nvpara),
        "den_range": [float(np.min(dist.den)), float(np.max(dist.den))],
        "sigma_theta": args.sigma_theta,
        "v_width": args.v_width,
        "f_g_stats": fg_stats,
        "outputs": outputs,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
