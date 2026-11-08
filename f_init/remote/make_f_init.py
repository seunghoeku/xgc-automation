#!/usr/bin/env python3
"""Login-node step: XGCa f0 snapshot -> XGC ``f_init`` distribution files.

Reproduces the "make distribution" cell of the f_init notebook with one
smoothing setting. Writes ``xgc.f_init_no_smooth[<sol>].bp`` and, when either
smoothing width is positive, ``xgc.f_init_smooth_<sigma>_<width>[<sol>].bp``.
The last stdout line is ``FINIT_JSON {...}`` for the local driver.

Open field lines (``--sol``): ``maxwellian`` zeroes f_g on the SOL beyond a
half-cosine taper of ``--sol-taper-psin`` past the separatrix, and in the
private region and on wall nodes, before either file is written, so both
start the SOL from the Maxwellian of the stored moments; ``keep`` leaves the
relaxed SOL in. Poloidal smoothing of whatever is left on open field lines
uses XGC_reader's open-line kernel (``--open-smooth shrink``: arc-length
Gaussian truncated at the plates), or the old padded index kernel
(``legacy``) for comparisons.

Time average (``--steps``): f from several f0 snapshots is averaged before
the Maxwellian is removed. The Maxwellian uses the initial moments stored in
``xgc.f0.mesh.bp`` (the same for every snapshot), so this is the average of
f_g. It removes the phase of slow oscillations (GAMs, edge Er) that a single
snapshot freezes in, and averages down marker noise.

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
    parser.add_argument("--steps", type=int, nargs="+", default=None,
                        help="average f over these f0 steps (overrides --step)")
    parser.add_argument("--var-string", default="i_f")
    parser.add_argument("--mass-au", type=float, default=2.0)
    parser.add_argument("--charge-num", type=float, default=1.0)
    parser.add_argument("--has-electron", action="store_true")
    parser.add_argument("--sigma-theta", type=float, default=0.0)
    parser.add_argument("--v-width", type=float, default=0.0)
    parser.add_argument("--sol", choices=("maxwellian", "keep"), default="maxwellian",
                        help="open field lines: Maxwellian (f_g zeroed beyond the taper) or keep the relaxed f_g")
    parser.add_argument("--sol-taper-psin", type=float, default=0.02,
                        help="half-cosine taper width in psi_N past the separatrix (0: step)")
    parser.add_argument("--open-smooth", choices=("shrink", "legacy"), default="shrink",
                        help="poloidal kernel on open field lines (XGC_reader open_mode)")
    parser.add_argument("--zero-nodes", type=int, nargs="+", default=None,
                        help="0-based mesh nodes whose f_g is set to zero (Maxwellian) before "
                             "smoothing, e.g. a transient pile-up caught in a time average")
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
    # The no-smooth file is the same for every smoothing setting of one f0
    # step (and one SOL setting, which is part of its name), so a smoothing
    # scan keeps the existing copy instead of clashing on it; only the
    # requested smooth file (or the no-smooth file when nothing else is
    # requested) is protected against overwriting.
    reuse_no_smooth = do_smooth and os.path.exists(targets[0]) and not args.overwrite
    protected = targets[1:] if reuse_no_smooth else targets
    clashes = [t for t in protected if os.path.exists(t)]
    if clashes and not args.overwrite:
        emit({"error": "exists", "paths": clashes})
        return 2

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    for name in ("set_sol_maxwellian", "split_open_surface"):
        if not hasattr(xd, name):
            emit({"error": "xgc_reader_too_old",
                  "message": f"XGC_reader at {args.xgc_reader} has no {name}; pull the checkout"})
            return 2

    if args.steps:
        steps = sorted(set(args.steps))
    else:
        steps = [args.step if args.step is not None else last_f0_step(run_dir)]
    f0_files = [f"xgc.f0.{st:05d}.bp" for st in steps]
    missing = [os.path.join(run_dir, f) for f in f0_files if not os.path.exists(os.path.join(run_dir, f))]
    if missing:
        emit({"error": "missing", "paths": missing})
        return 2
    step, f0_file = steps[-1], f0_files[-1]

    x = _xgc1(xgc_reader, run_dir)   # note: chdir into run_dir
    x.load_unitsm()
    x.setup_mesh()
    x.setup_f0mesh()

    def read(name):
        return xd.XGCDistribution.from_xgc_output(
            name,
            time_step=0,
            mass_au=args.mass_au,
            charge_num=args.charge_num,
            var_string=args.var_string,
            dir=run_dir,
            has_electron=args.has_electron,
            use_initial_moments=True,
        )

    dist = read(f0_files[0])
    if len(f0_files) > 1:
        acc = np.array(dist.f, dtype=np.float64)
        for name in f0_files[1:]:
            acc += np.asarray(read(name).f, dtype=np.float64)
        dist.f = acc / len(f0_files)
    dist.remove_maxwellian()

    region = getattr(x.mesh, "region", None)
    sol_info: dict = {"mode": args.sol, "taper_psin": args.sol_taper_psin, "applied": False}
    if args.sol == "maxwellian":
        if region is None:
            # A mesh without a region array has no open field lines XGC
            # could tell apart (closed-geometry cases); nothing to do.
            sol_info["note"] = "no region in xgc.mesh.bp; nothing zeroed"
        else:
            weight = xd.set_sol_maxwellian(dist, x, taper_psin=args.sol_taper_psin)
            sol_info["applied"] = True
            sol_info["nodes_zeroed"] = int(np.sum(weight == 0))
            sol_info["nodes_tapered"] = int(np.sum((weight > 0) & (weight < 1)))

    zero_info = None
    if args.zero_nodes:
        nodes = sorted(set(args.zero_nodes))
        bad = [n for n in nodes if n < 0 or n >= dist.nnodes]
        if bad:
            emit({"error": "bad_nodes", "nodes": bad, "nnodes": int(dist.nnodes)})
            return 2
        before = [float(np.max(np.abs(dist.f_g[n]))) for n in nodes]
        dist.f_g[nodes] = 0.0
        zero_info = {"nodes": nodes, "abs_max_before": before}

    def region_rms(f_g: "np.ndarray") -> dict:
        if region is None:
            return {}
        rg = np.asarray(region)
        out = {}
        for label, msk in (("closed", rg == 1), ("sol", rg == 2), ("private", rg == 3), ("wall", rg >= 100)):
            if msk.any():
                out[label] = float(np.sqrt(np.mean(f_g[msk] ** 2)))
        return out

    outputs = []
    if reuse_no_smooth:
        outputs.append({"kind": "no_smooth", "path": targets[0], "reused": True})
    else:
        dist.save(targets[0])
        outputs.append({"kind": "no_smooth", "path": targets[0]})

    fg_stats = {
        "no_smooth": {
            "abs_max": float(np.max(np.abs(dist.f_g))),
            "rms": float(np.sqrt(np.mean(dist.f_g**2))),
            "rms_by_region": region_rms(dist.f_g),
        }
    }
    if do_smooth:
        smooth = copy.deepcopy(dist)
        if args.sigma_theta > 0:
            xd.poloidal_smooth_f_init(smooth, x, sigma_theta=args.sigma_theta, open_mode=args.open_smooth)
        if args.v_width > 0:
            xd.vspace_smooth(smooth, width=args.v_width)
        smooth.save(targets[1])
        outputs.append({"kind": "smooth", "path": targets[1]})
        fg_stats["smooth"] = {
            "abs_max": float(np.max(np.abs(smooth.f_g))),
            "rms": float(np.sqrt(np.mean(smooth.f_g**2))),
            "rms_by_region": region_rms(smooth.f_g),
        }

    emit({
        "f0_step": step,
        "f0_steps": steps,
        "f0_file": os.path.join(run_dir, f0_file),
        "nnodes": int(dist.nnodes),
        "nvperp": int(dist.vgrid.nvperp),
        "nvpara": int(dist.vgrid.nvpara),
        "den_range": [float(np.min(dist.den)), float(np.max(dist.den))],
        "sigma_theta": args.sigma_theta,
        "v_width": args.v_width,
        "sol": sol_info,
        "zero_nodes": zero_info,
        "open_smooth": args.open_smooth if args.sigma_theta > 0 else None,
        "f_g_stats": fg_stats,
        "outputs": outputs,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
