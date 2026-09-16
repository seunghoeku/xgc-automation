#!/usr/bin/env python3
"""Login-node step: time-average ``iden``/``eden`` of an XGC1 run into one file.

``adjust`` compares the density the dt=0 run reconstructs from ``f_init``
against the density of the state the f_init was taken from. When that state is
a turbulence run (case 3) the f_init is a time average over several f0
snapshots, so the reference must be averaged over the same window -- comparing
it against a single snapshot would put back exactly the fluctuation the
average was there to remove.

This writes ``xgc.3d.<step>.bp`` holding nothing but the averaged densities, so
``adjust``'s existing ``xgc.3d`` path picks it up with no special casing. Name
the output step after the f_init's own step and ``--ref-step`` is not needed.

The last stdout line is ``FINIT_JSON {...}`` for the local driver.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DENSITY_VARS = ("iden", "eden")


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="XGC1 run holding xgc.3d.*.bp")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--steps", type=int, nargs="+", required=True,
                        help="xgc.3d steps to average; use the ones nearest the "
                             "f0 steps that went into the f_init")
    parser.add_argument("--out-step", type=int, required=True,
                        help="step number of the written file; match the f_init's")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    import adios2
    import numpy as np

    run_dir = os.path.abspath(args.run_dir)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    missing = [
        os.path.join(run_dir, f"xgc.3d.{s:05d}.bp")
        for s in args.steps
        if not os.path.exists(os.path.join(run_dir, f"xgc.3d.{s:05d}.bp"))
    ]
    if missing:
        emit({"error": "missing", "paths": missing})
        return 2

    target = os.path.join(out_dir, f"xgc.3d.{args.out_step:05d}.bp")
    if os.path.exists(target) and not args.overwrite:
        emit({"error": "exists", "paths": [target]})
        return 2

    sums: dict[str, "np.ndarray"] = {}
    present: list[str] = []
    for step in args.steps:
        with adios2.FileReader(os.path.join(run_dir, f"xgc.3d.{step:05d}.bp")) as f:
            names = f.available_variables()
            if not present:
                present = [v for v in DENSITY_VARS if v in names]
                if not present:
                    emit({"error": "no_density_variable", "paths": [run_dir]})
                    return 2
            for var in present:
                # (nphi, nnode); the toroidal average is left to read_iden in
                # adjust_f_init.py, which does it for any 2-D iden.
                arr = np.asarray(f.read(var), dtype=np.float64)
                sums[var] = arr if var not in sums else sums[var] + arr

    stats = {}
    with adios2.Stream(target, "w") as s:
        for var in present:
            arr = np.ascontiguousarray(sums[var] / float(len(args.steps)))
            s.write(var, arr, list(arr.shape), [0] * arr.ndim, list(arr.shape))
            toroidal = arr.mean(axis=0) if arr.ndim == 2 else arr
            stats[var] = {
                "shape": list(arr.shape),
                "toroidal_avg_range": [float(toroidal.min()), float(toroidal.max())],
            }
        for name, value in (("step", args.out_step), ("gstep", args.out_step),
                            ("tindex", args.out_step)):
            s.write(name, np.asarray(value, dtype=np.int32))

    emit({
        "run_dir": run_dir,
        "out_file": target,
        "steps_averaged": list(args.steps),
        "out_step": args.out_step,
        "density_vars": present,
        "stats": stats,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
