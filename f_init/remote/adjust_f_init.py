#!/usr/bin/env python3
"""Login-node step: dt=0 charge-density check and f_init density correction.

Compares the ion density XGCa produced at the end of the Maxwellian-start run
(``xgc.2d.<ref>.bp`` in the run directory) with the density the dt=0 run
reconstructs from the ``f_init`` file (``xgc.2d.<cmp>.bp`` in the dt0
directory). The difference is flux-surface averaged, scattered back to the
mesh, and the full distribution is scaled node by node::

    f_adjusted = f_init_total * (1 + factor * <n_ref - n_dt0>_fsa / n_local)

where ``n_local`` is the density moment of ``f_init`` itself. The analytic
Maxwellian is then removed again and the result saved as a new f_init file.
``--check-only`` computes the mismatch metrics without writing, which is how
an adjusted iteration is verified. Last stdout line: ``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def steps_in(directory: str, prefix: str) -> list[int]:
    steps = []
    for path in glob.glob(os.path.join(directory, f"{prefix}.[0-9]*.bp")):
        match = re.search(rf"{re.escape(prefix)}\.(\d+)\.bp$", path)
        if match:
            steps.append(int(match.group(1)))
    return sorted(steps)


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="XGCa (Maxwellian start) run")
    parser.add_argument("--dt0-dir", required=True, help="dt=0 run started from f_init")
    parser.add_argument("--finit", required=True, help="f_init file the dt0 run used")
    parser.add_argument("--ref-step", type=int, default=None,
                        help="xgc.2d step in run dir; default: the last f0 step")
    parser.add_argument("--cmp-step", type=int, default=None,
                        help="xgc.2d step in dt0 dir; default: first available")
    parser.add_argument("--factor", type=float, default=1.0)
    parser.add_argument("--out", default=None, help="adjusted f_init path")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--png-out", default=None)
    parser.add_argument("--region-max", type=int, default=1,
                        help="mesh regions <= this are scored and rescaled "
                             "(1 = closed flux surfaces only, SOL untouched; 2 = also SOL)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    dt0_dir = os.path.abspath(args.dt0_dir)
    finit = os.path.abspath(args.finit)
    out = os.path.abspath(args.out) if args.out else None

    if not args.check_only:
        if not out:
            raise SystemExit("--out is required unless --check-only")
        if os.path.exists(out) and not args.overwrite:
            emit({"error": "exists", "paths": [out]})
            return 2
    if not os.path.exists(finit):
        emit({"error": "missing", "paths": [finit]})
        return 2

    ref_steps = steps_in(run_dir, "xgc.2d")
    cmp_steps = steps_in(dt0_dir, "xgc.2d")
    if not ref_steps or not cmp_steps:
        emit({"error": "missing", "paths": [
            f"{run_dir}/xgc.2d.*.bp" if not ref_steps else f"{dt0_dir}/xgc.2d.*.bp"]})
        return 2
    if args.ref_step is None:
        f0_steps = steps_in(run_dir, "xgc.f0")
        ref_step = max(f0_steps) if f0_steps and max(f0_steps) in ref_steps else ref_steps[-1]
    else:
        ref_step = args.ref_step
    cmp_step = args.cmp_step if args.cmp_step is not None else cmp_steps[0]
    for directory, step in ((run_dir, ref_step), (dt0_dir, cmp_step)):
        if not os.path.exists(os.path.join(directory, f"xgc.2d.{step:05d}.bp")):
            emit({"error": "missing", "paths": [os.path.join(directory, f"xgc.2d.{step:05d}.bp")]})
            return 2

    sys.path.insert(0, args.xgc_reader)
    import adios2
    import numpy as np
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    def read_iden(path: str) -> np.ndarray:
        with adios2.FileReader(path) as fh:
            iden = np.asarray(fh.read("iden"))
        # (nphi, nnodes) for XGC1-style output; XGCa writes one plane.
        return np.mean(np.atleast_2d(iden), axis=0)

    iden_ref = read_iden(os.path.join(run_dir, f"xgc.2d.{ref_step:05d}.bp"))
    iden_cmp = read_iden(os.path.join(dt0_dir, f"xgc.2d.{cmp_step:05d}.bp"))

    x1 = xgc_reader.xgc1(path=run_dir)   # note: chdir into run_dir
    x1.load_unitsm()
    x1.setup_mesh()
    x1.setup_f0mesh()

    delta_n = iden_ref - iden_cmp

    def fsa_scatter(var: np.ndarray) -> np.ndarray:
        # Per-surface volume-weighted average scattered back through the
        # surface membership lists (surf_idx/surf_len). xgc_reader's
        # flux_surface_average interpolates surface values onto node psi
        # instead; with private-flux surfaces appended after the SOL ones in
        # psi_surf that interpolation is not monotonic and hands far-SOL nodes
        # a private-region value. Nodes on no surface stay NaN.
        surf = x1.fsa_simple(var)
        out = np.full(var.shape, np.nan)
        for i in range(surf.size):
            length = int(x1.mesh.surf_len[i])
            if length > 0:
                out[np.asarray(x1.mesh.surf_idx[i, :length]) - 1] = surf[i]
        return out

    dn_fsa = fsa_scatter(delta_n)

    dist = xd.XGCDistribution.from_xgc_input(finit)
    den, _temp, _flow = dist.update_maxwellian_moments(
        x1, do_flux_average=False, no_update=True
    )   # adds the Maxwellian back: dist.f now holds the full distribution

    # `iden` in xgc.2d is the perturbed gyrocenter density (order 1e17 m^-3 on
    # cbc6, crossing zero), so every relative measure is taken against the
    # total density of f_init itself, not against iden.
    region = getattr(x1.mesh, "region", None)
    mask = np.ones(dist.nnodes, dtype=bool)
    if region is not None:
        mask &= np.asarray(region) <= args.region_max
    mask &= den > 0
    on_surface = np.isfinite(dn_fsa)
    n_off_surface = int(np.sum(mask & ~on_surface))
    mask &= on_surface   # nodes on no stored flux surface: no metric, no scaling
    dn_fsa = np.where(on_surface, dn_fsa, 0.0)

    rel = np.zeros_like(dn_fsa)
    rel[mask] = dn_fsa[mask] / den[mask]
    raw = np.zeros_like(delta_n)
    raw[den > 0] = delta_n[den > 0] / den[den > 0]

    def stats(values: np.ndarray) -> dict:
        v = values[mask]
        return {
            "rms": float(np.sqrt(np.mean(v**2))),
            "abs_max": float(np.max(np.abs(v))),
            "mean": float(np.mean(v)),
        }

    metrics = {
        "raw_dn_over_n": stats(raw),
        "fsa_dn_over_n": stats(rel),
        "raw_dn_m3": stats(delta_n),
        "fsa_dn_m3": stats(dn_fsa),
        "iden_ref_range_m3": [float(iden_ref[mask].min()), float(iden_ref[mask].max())],
        "iden_dt0_range_m3": [float(iden_cmp[mask].min()), float(iden_cmp[mask].max())],
        "finit_density_range_m3": [float(den[mask].min()), float(den[mask].max())],
        "n_nodes_in_metric": int(mask.sum()),
        "n_nodes_off_surface_excluded": n_off_surface,
    }

    payload = {
        "run_dir": run_dir,
        "dt0_dir": dt0_dir,
        "finit": finit,
        "ref_step": ref_step,
        "cmp_step": cmp_step,
        "factor": args.factor,
        "check_only": bool(args.check_only),
        "nnodes": int(dist.nnodes),
        "metrics": metrics,
        "output": None,
    }

    if not args.check_only:
        scale = 1.0 + args.factor * rel
        scale[~mask] = 1.0   # no correction outside the metric region
        dist.f *= scale[:, np.newaxis, np.newaxis]
        den_adj, _t, _f = dist.update_maxwellian_moments(
            x1, do_flux_average=False, no_update=True
        )
        dist.remove_maxwellian()
        dist.save(out)
        change = np.zeros_like(den)
        change[den > 0] = (den_adj[den > 0] - den[den > 0]) / den[den > 0]
        metrics["applied_density_change"] = stats(change)
        metrics["scale_range"] = [float(scale[mask].min()), float(scale[mask].max())]
        payload["output"] = out

    if args.png_out:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            psi_n = x1.mesh.psi / x1.psix
            fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
            axes[0].plot(psi_n[mask], raw[mask], ".", ms=2, alpha=0.4, label="node-wise")
            axes[0].plot(psi_n[mask], rel[mask], ".", ms=3, label="flux-surface avg")
            axes[0].axhline(0, color="k", lw=0.5)
            axes[0].set_xlabel("psi_n")
            axes[0].set_ylabel("(n_ref - n_dt0) / n_finit")
            axes[0].set_title(f"density mismatch  ref step {ref_step}, dt0 step {cmp_step}")
            axes[0].legend()
            try:
                tri = x1.mesh.triobj
                cf = axes[1].tricontourf(tri, dn_fsa, levels=40, cmap="RdBu_r")
                fig.colorbar(cf, ax=axes[1], label="<n_ref - n_dt0>_fsa [m^-3]")
                axes[1].set_aspect("equal")
                axes[1].set_xlabel("R [m]")
                axes[1].set_ylabel("Z [m]")
            except Exception as exc:  # noqa: BLE001 - plot is best effort
                axes[1].text(0.1, 0.5, f"2D plot unavailable: {exc}", wrap=True)
            fig.suptitle(os.path.basename(finit))
            fig.tight_layout()
            fig.savefig(args.png_out, dpi=110)
            payload["png"] = args.png_out
        except Exception as exc:  # noqa: BLE001 - never fail the step on plotting
            payload["png_error"] = str(exc)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        payload["json"] = args.json_out

    emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
