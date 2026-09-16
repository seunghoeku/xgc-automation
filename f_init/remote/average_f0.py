#!/usr/bin/env python3
"""Login-node step: XGC1 turbulence f0 snapshots -> one XGCa-shaped f0 file.

The f_init procedure (``make_f_init.py``) expects the f0 snapshot of an
axisymmetric XGCa run. When the relaxed distribution to start from is the
turbulence run's own f0 instead, two things are in the way:

* the XGC1 array carries a toroidal dimension, ``(nphi, nmu+1, nnode, nvp)``;
* one snapshot of a production mesh is ~100 GB, so neither the toroidal
  average nor a time average fits in a login node's memory in one piece.

This script removes both. It streams each snapshot one toroidal plane at a
time, accumulates the sum over planes and over the requested snapshots, and
writes ``xgc.f0.<step>.bp`` with ``nphi=1`` -- byte-for-byte the shape XGCa
writes, so ``XGCDistribution.from_xgc_output`` reads it unchanged. Peak memory
is one plane of one species (about 1 GB on a 137k-node mesh).

Averaging over a handful of snapshots is what makes the result usable as an
initial condition: a single turbulent snapshot carries the instantaneous
fluctuation, and the f_init is meant to be the relaxed background.

The last stdout line is ``FINIT_JSON {...}`` for the local driver.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

# Variables averaged over the toroidal direction and written back with the
# XGCa rank: the distributions keep (1, nmu+1, nnode, nvp), the f0-grid
# densities keep (1, nnode), and dpot collapses to (nnode,) the way XGCa
# writes it (from_xgc_output only averages dpot when it is still 2-D).
SPECIES_VARS = ("e_f", "i_f", "i2f", "i3f", "i4f")
DENSITY_VARS = ("eden_f0", "iden_f0_approx")


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def available_steps(run_dir: str) -> list[int]:
    steps = []
    for path in glob.glob(os.path.join(run_dir, "xgc.f0.[0-9]*.bp")):
        match = re.search(r"xgc\.f0\.(\d+)\.bp$", path)
        if match:
            steps.append(int(match.group(1)))
    return sorted(steps)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="XGC1 turbulence run directory")
    parser.add_argument("--out-dir", required=True, help="where the averaged f0 is written")
    parser.add_argument(
        "--steps",
        type=int,
        nargs="+",
        default=None,
        help="f0 steps to average; default: the first --n-steps available",
    )
    parser.add_argument("--n-steps", type=int, default=4, help="used when --steps is absent")
    parser.add_argument(
        "--out-step",
        type=int,
        default=None,
        help="step number written into the output file name and its 'step' "
        "variable; default: the last averaged step",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    import adios2
    import numpy as np

    run_dir = os.path.abspath(args.run_dir)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    steps = args.steps
    if steps is None:
        found = available_steps(run_dir)
        if not found:
            emit({"error": "missing", "paths": [os.path.join(run_dir, "xgc.f0.<step>.bp")]})
            return 2
        steps = found[: args.n_steps]
    missing = [
        os.path.join(run_dir, f"xgc.f0.{s:05d}.bp")
        for s in steps
        if not os.path.exists(os.path.join(run_dir, f"xgc.f0.{s:05d}.bp"))
    ]
    if missing:
        emit({"error": "missing", "paths": missing})
        return 2

    out_step = args.out_step if args.out_step is not None else max(steps)
    target = os.path.join(out_dir, f"xgc.f0.{out_step:05d}.bp")
    if os.path.exists(target) and not args.overwrite:
        emit({"error": "exists", "paths": [target]})
        return 2

    sums: dict[str, "np.ndarray"] = {}
    scalars: dict[str, object] = {}
    nphi = None
    present: list[str] = []
    times: list[float] = []

    for step in steps:
        path = os.path.join(run_dir, f"xgc.f0.{step:05d}.bp")
        with adios2.FileReader(path) as f:
            names = f.available_variables()
            if nphi is None:
                nphi = int(f.read("nphi"))
                present = [v for v in SPECIES_VARS if v in names]
                if not present:
                    emit({"error": "no_species_variable", "paths": [path]})
                    return 2
                # Scalars describing the velocity grid and the mesh are the
                # same in every snapshot; take them from the first one.
                for name in ("nnode", "ndata", "mudata", "vpdata", "nmup1",
                             "imu1m1", "inode1m1", "iphi"):
                    if name in names:
                        scalars[name] = f.read(name)
            elif int(f.read("nphi")) != nphi:
                emit({"error": "nphi_mismatch", "paths": [path]})
                return 2

            if "time" in names:
                times.append(float(f.read("time")))

            for var in present:
                shape = [int(x) for x in names[var]["Shape"].split(",")]
                # (nphi, nmu+1, nnode, nvp): read and accumulate plane by plane
                # so peak memory stays at one plane.
                for iphi in range(shape[0]):
                    plane = f.read(
                        var,
                        start=[iphi, 0, 0, 0],
                        count=[1, shape[1], shape[2], shape[3]],
                    )
                    acc = sums.get(var)
                    if acc is None:
                        sums[var] = np.array(plane, dtype=np.float64)
                    else:
                        acc += plane

            for var in DENSITY_VARS:
                if var not in names:
                    continue
                arr = np.asarray(f.read(var), dtype=np.float64)
                # (nphi, nnode) -> (1, nnode)
                red = arr.mean(axis=0, keepdims=True) if arr.ndim == 2 else arr.reshape(1, -1)
                sums[var] = red if var not in sums else sums[var] + red

            if "dpot" in names:
                arr = np.asarray(f.read("dpot"), dtype=np.float64)
                # (nphi, nnode) -> (nnode,), the rank XGCa writes
                red = arr.mean(axis=0) if arr.ndim == 2 else arr
                sums["dpot"] = red if "dpot" not in sums else sums["dpot"] + red

    n_snapshots = len(steps)
    for var in present:
        sums[var] /= float(nphi * n_snapshots)
    for var in list(DENSITY_VARS) + ["dpot"]:
        if var in sums:
            sums[var] /= float(n_snapshots)

    stats = {}
    with adios2.Stream(target, "w") as s:
        for var in present:
            arr = np.ascontiguousarray(sums[var])
            s.write(var, arr, list(arr.shape), [0] * arr.ndim, list(arr.shape))
            stats[var] = {
                "shape": list(arr.shape),
                "min": float(arr.min()),
                "max": float(arr.max()),
                "sum": float(arr.sum()),
            }
        for var in list(DENSITY_VARS) + ["dpot"]:
            if var not in sums:
                continue
            arr = np.ascontiguousarray(sums[var])
            s.write(var, arr, list(arr.shape), [0] * arr.ndim, list(arr.shape))
        for name, value in scalars.items():
            s.write(name, np.asarray(value))
        # nphi is 1 now; the averaged file must say so, or a reader that trusts
        # the scalar over the array shape would slice the wrong way.
        s.write("nphi", np.asarray(1, dtype=np.int32))
        # `step` is the XGC1 spelling, `gstep`/`tindex` the XGCa one;
        # step_var_name() in XGC_reader accepts either, so write both.
        for name in ("step", "gstep", "tindex"):
            s.write(name, np.asarray(out_step, dtype=np.int32))
        s.write("time", np.asarray(times[-1] if times else 0.0, dtype=np.float64))

    emit({
        "run_dir": run_dir,
        "out_file": target,
        "steps_averaged": steps,
        "n_snapshots": n_snapshots,
        "nphi_averaged": nphi,
        "out_step": out_step,
        "species_vars": present,
        "time_range": [times[0], times[-1]] if times else None,
        "stats": stats,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
