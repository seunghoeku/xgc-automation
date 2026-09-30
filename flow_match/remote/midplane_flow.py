#!/usr/bin/env python3
"""Login-node step: carbon parallel flow at the outboard midplane of an XGCa run.

Reads the per-step moment files in a run directory, ``xgc.f2d.<step>.bp``
(XGCa, one plane) or ``xgc.f3d.<step>.bp`` (XGC1), or a single stepped file,, takes the parallel bulk flow of one
species (default ``i2u_para``, the third species, carbon), averages it over
a window of the last steps, and interpolates the mesh field along the
outboard midplane ``Z = Z_axis``, ``R > R_axis`` with a linear triangular
interpolator. The same line gives ``psi_n``, ``R`` and ``B_phi / B`` from
``xgc.mesh.bp``, ``xgc.equil.bp`` and ``xgc.bfield.bp``.

With XGC's default ``eq_flow_type = 2`` the input rotation profile
``omega(psi)`` (rad/s) enters the code as ``u_par = omega * R * B_phi / B``,
so the quantity that is compared with the input file is

    omega_sim = u_para / (R * B_phi / B)        [rad/s]
    v_tor     = u_para * B_phi / B              [m/s]   (for the CER comparison)

Saturation is judged from the per-step value at a few probe surfaces: the
relative change between the first and second half of the averaging window
is reported, and the caller decides whether to trust the average.

Outputs: a JSON summary, a ``.prf`` of ``omega_sim(psi_n)``, and a PNG.
Last stdout line: ``FLOWMATCH_JSON {...}``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def emit(payload: dict) -> None:
    print("FLOWMATCH_JSON " + json.dumps(payload), flush=True)


def fail(message: str, **extra) -> None:
    payload = {"error": message}
    payload.update(extra)
    emit(payload)
    sys.exit(1)


DIAG_PREFIXES = ("f2d", "f3d")


def diag_steps(run_dir: str) -> tuple[str, list[int]]:
    """(prefix, steps) of the per-step moment files: XGCa writes ``xgc.f2d.<step>.bp``
    (one plane), XGC1 ``xgc.f3d.<step>.bp``; both carry ``<species>u_para``."""
    for prefix in DIAG_PREFIXES:
        steps = []
        for path in glob.glob(os.path.join(run_dir, f"xgc.{prefix}.[0-9]*.bp")):
            match = re.search(rf"xgc\.{prefix}\.(\d+)\.bp$", path)
            if match:
                steps.append(int(match.group(1)))
        if steps:
            return prefix, sorted(steps)
    return DIAG_PREFIXES[-1], []


def read_var(reader, name):
    import numpy as np

    return np.asarray(reader.read(name))


def load_geometry(run_dir: str):
    """psi_n, R, Z, B_phi/B on the mesh nodes plus the axis position."""
    import adios2
    import numpy as np

    with adios2.FileReader(os.path.join(run_dir, "xgc.mesh.bp")) as f:
        rz = read_var(f, "rz")
        psi = read_var(f, "psi")
        connect = read_var(f, "nd_connect_list")
    with adios2.FileReader(os.path.join(run_dir, "xgc.equil.bp")) as f:
        axis_r = float(read_var(f, "eq_axis_r"))
        axis_z = float(read_var(f, "eq_axis_z"))
        x_psi = float(read_var(f, "eq_x_psi"))
    with adios2.FileReader(os.path.join(run_dir, "xgc.bfield.bp")) as f:
        bfield = read_var(f, "bfield")
    # bfield is (3, nnode) = (B_R, B_Z, B_phi); older files may be (nnode, 3).
    if bfield.shape[0] != 3 and bfield.shape[-1] == 3:
        bfield = bfield.T
    bmag = np.sqrt((bfield**2).sum(axis=0))
    bphi_over_b = bfield[2] / bmag
    r = rz[:, 0]
    z = rz[:, 1]
    return {
        "r": r,
        "z": z,
        "psi_n": psi / x_psi,
        "bphi_over_b": bphi_over_b,
        "axis_r": axis_r,
        "axis_z": axis_z,
        "connect": connect - 1 if connect.min() == 1 else connect,
    }


def midplane_interpolator(geom, nr: int):
    """Outboard-midplane sample points and a factory for node-field interpolation."""
    import numpy as np
    from matplotlib.tri import LinearTriInterpolator, Triangulation

    tri = Triangulation(geom["r"], geom["z"], triangles=geom["connect"])
    r_line = np.linspace(geom["axis_r"], geom["r"].max(), nr)
    z_line = np.full_like(r_line, geom["axis_z"])

    def sample(field):
        values = LinearTriInterpolator(tri, field)(r_line, z_line)
        return values

    psi_line = sample(geom["psi_n"])
    mask = ~np.ma.getmaskarray(psi_line)
    return r_line[mask], np.asarray(psi_line[mask]), mask, sample


def read_u_para(run_dir: str, prefix: str, steps: list[int], var: str):
    """{step: u_para(nnode)} from per-step files, or from a stepped single file."""
    import adios2
    import numpy as np

    result = {}
    if steps:
        for step in steps:
            path = os.path.join(run_dir, f"xgc.{prefix}.{step:05d}.bp")
            with adios2.FileReader(path) as f:
                names = f.available_variables()
                if var not in names:
                    fail(
                        f"{var} not in {os.path.basename(path)}",
                        available=sorted(n for n in names if n.endswith("u_para")),
                    )
                arr = read_var(f, var)
                # XGCa writes (nnode,) or (1, nnode); a toroidal axis is averaged.
                result[step] = arr.reshape(-1, arr.shape[-1]).mean(axis=0)
        return result
    path = os.path.join(run_dir, f"xgc.{prefix}.bp")
    if not os.path.isdir(path):
        fail("no xgc.f2d/f3d.<step>.bp files and no stepped xgc.f2d/f3d.bp in run directory")
    with adios2.Stream(path, "rra") as stream:
        for _ in stream.steps():
            names = stream.available_variables()
            if var not in names:
                fail(
                    f"{var} not in xgc.f3d.bp",
                    available=sorted(n for n in names if n.endswith("u_para")),
                )
            arr = np.asarray(stream.read(var))
            step = int(stream.current_step())
            result[step] = arr.reshape(-1, arr.shape[-1]).mean(axis=0)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--var", default="i2u_para", help="f2d/f3d parallel-flow variable")
    parser.add_argument("--last", type=int, default=500, help="average over the last N steps")
    parser.add_argument("--step-min", type=int, default=None, help="explicit window start (overrides --last)")
    parser.add_argument("--step-max", type=int, default=None, help="explicit window end")
    parser.add_argument("--nr", type=int, default=400, help="midplane sample points")
    parser.add_argument("--probes", default="0.3,0.6,0.8,0.9,0.95", help="psi_n probe surfaces")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--prf-out", required=True, help="omega_sim(psi_n) profile file")
    parser.add_argument("--png-out", default=None)
    args = parser.parse_args(argv)

    import numpy as np

    run_dir = os.path.abspath(args.run_dir)
    if not os.path.isdir(run_dir):
        fail(f"run directory not found: {run_dir}")

    prefix, steps = diag_steps(run_dir)
    fields = read_u_para(run_dir, prefix, steps, args.var)
    all_steps = sorted(fields)
    if not all_steps:
        fail("no f3d steps found")
    step_max = args.step_max if args.step_max is not None else all_steps[-1]
    step_min = args.step_min if args.step_min is not None else step_max - args.last
    window = [s for s in all_steps if step_min <= s <= step_max]
    if not window:
        fail("averaging window is empty", steps=all_steps, step_min=step_min, step_max=step_max)

    geom = load_geometry(run_dir)
    r_mid, psi_mid, mask, sample = midplane_interpolator(geom, args.nr)
    bphi_over_b_mid = np.asarray(sample(geom["bphi_over_b"])[mask])
    geometry_factor = r_mid * bphi_over_b_mid  # u_par = omega * R * Bphi/B

    u_mean = np.mean([fields[s] for s in window], axis=0)
    u_mid = np.asarray(sample(u_mean)[mask])
    omega_sim = u_mid / geometry_factor
    vtor_sim = u_mid * bphi_over_b_mid

    # Saturation probes: omega at fixed psi_n for every step in the window.
    probes = [float(p) for p in args.probes.split(",") if p.strip()]
    series = {}
    for step in window:
        u_line = np.asarray(sample(fields[step])[mask])
        omega_line = u_line / geometry_factor
        series[step] = [float(np.interp(p, psi_mid, omega_line)) for p in probes]
    half = len(window) // 2
    saturation = []
    for k, p in enumerate(probes):
        first = float(np.mean([series[s][k] for s in window[:half]])) if half else None
        second = float(np.mean([series[s][k] for s in window[half:]]))
        scale = max(abs(second), 1e-30)
        saturation.append(
            {
                "psi_n": p,
                "omega_first_half": first,
                "omega_second_half": second,
                "relative_change": (second - first) / scale if first is not None else None,
            }
        )

    with open(args.prf_out, "w", encoding="utf-8") as fh:
        fh.write(f"{psi_mid.size}\n")
        for x, y in zip(psi_mid, omega_sim):
            fh.write(f"{x:19.13e}  {y:19.13e}\n")
        fh.write("-1\n")

    payload = {
        "run_dir": run_dir,
        "var": args.var,
        "diag_prefix": prefix,
        "steps_available": [all_steps[0], all_steps[-1], len(all_steps)],
        "window": {"step_min": window[0], "step_max": window[-1], "count": len(window)},
        "axis": {"r": geom["axis_r"], "z": geom["axis_z"]},
        "midplane": {
            "psi_n": psi_mid.tolist(),
            "r": r_mid.tolist(),
            "bphi_over_b": bphi_over_b_mid.tolist(),
            "u_para": u_mid.tolist(),
            "omega": omega_sim.tolist(),
            "vtor": vtor_sim.tolist(),
        },
        "probes": probes,
        "series": {str(s): v for s, v in series.items()},
        "saturation": saturation,
        "prf": args.prf_out,
        "json": args.json_out,
        "png": {},
    }

    if args.png_out:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        axes[0].plot(psi_mid, omega_sim / 1e3, label=f"{args.var} -> omega")
        axes[0].set_xlabel("psi_n")
        axes[0].set_ylabel("omega [krad/s]")
        axes[0].set_title(f"steps {window[0]}-{window[-1]} mean")
        axes[0].grid(True)
        for k, p in enumerate(probes):
            axes[1].plot(window, [series[s][k] / 1e3 for s in window], label=f"psi_n={p}")
        axes[1].set_xlabel("step")
        axes[1].set_ylabel("omega [krad/s]")
        axes[1].set_title("saturation probes")
        axes[1].grid(True)
        axes[1].legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(args.png_out, dpi=110)
        payload["png"]["midplane"] = args.png_out

    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    emit({k: v for k, v in payload.items() if k not in ("midplane", "series")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
