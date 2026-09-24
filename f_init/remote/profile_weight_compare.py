#!/usr/bin/env python3
"""Login-node step: profile and marker-weight comparison of f_init runs.

For stage A (the Maxwellian-start XGCa run) and each production run started
from an f_init file:

* flux-surface profiles from ``xgc.oneddiag.bp`` -- ion density, temperature
  (T = (2 T_perp + T_par)/3, T_par = 2 (<E_par> - m u_par^2 / 2)), parallel
  flow and poloidal ExB flow -- at the first record and at the last step all
  f_init runs share, relative to the stage A profile at the f0 extraction step
  (the state every f_init was built from) and to a reference f_init run;
* the psi-rms drift of density and temperature from that target over time;
* marker weights from ``xgc.particle.*.bp`` (iphase columns: 0 r, 1 z,
  3 rho_par, 4 w1, 6 mu, 7 w0): the rms of the delta-f weight w0*w1 (physical
  particles per marker) over time, against psi_N and against E/T, the w0
  spread at load, and the w0*w1 change of the same markers (by igid) between
  the first and last snapshot.

Reduced series go to ``<out-prefix>.json`` and figures to
``<out-prefix>_<name>.png``. Last stdout line: ``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

PROTON_MASS = 1.67262192e-27
E_CHARGE = 1.602176634e-19


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def particle_files(directory: str, max_step: int | None) -> list[tuple[int, str]]:
    out = []
    for path in glob.glob(os.path.join(directory, "xgc.particle.[0-9]*.bp")):
        match = re.search(r"xgc\.particle\.(\d+)\.bp$", path)
        if match:
            step = int(match.group(1))
            if max_step is None or step <= max_step:
                out.append((step, path))
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="stage A (Maxwellian start)")
    parser.add_argument("--dir", action="append", default=[],
                        help="NAME=PATH of an f_init production run (repeatable)")
    parser.add_argument("--ref", default=None, help="NAME of the reference f_init run")
    parser.add_argument("--f0-step", type=int, required=True)
    parser.add_argument("--mass-au", type=float, default=2.0)
    parser.add_argument("--psi-range", type=float, nargs=2, default=(0.05, 0.95),
                        help="psi_N range of the rms profile metrics")
    parser.add_argument("--nbins", type=int, default=20, help="psi_N bins of rms(w1)")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    runs_in = []
    for item in args.dir:
        name, sep, path = item.partition("=")
        if not sep:
            raise SystemExit(f"--dir {item!r}: expected NAME=PATH")
        runs_in.append((name, os.path.abspath(path)))
    if not runs_in:
        raise SystemExit("at least one --dir is needed")
    ref = args.ref or runs_in[0][0]
    if ref not in {n for n, _ in runs_in}:
        raise SystemExit(f"--ref {ref!r} is not one of the --dir names")

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import adios2
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.tri import LinearTriInterpolator
    from xgc_reader import base as xgc_reader

    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)
    mass = args.mass_au * PROTON_MASS
    stage_a = os.path.abspath(args.run_dir)

    def read_oned(directory: str, max_step: int | None):
        with adios2.FileReader(os.path.join(directory, "xgc.oneddiag.bp")) as fh:
            nsteps = int(fh.available_variables()["gstep"]["AvailableStepsCount"])
            def rd(name):
                return np.asarray(fh.read(name, step_selection=[0, nsteps]))
            psi = np.asarray(fh.read("psi")).ravel()   # first step; the grid is fixed
            data = {
                "gstep": rd("gstep").ravel().astype(int),
                "time": rd("time").ravel(),
                "n": rd("i_gc_density_df_1d").reshape(nsteps, -1),
                "tperp": rd("i_perp_temperature_df_1d").reshape(nsteps, -1),
                "epar": rd("i_parallel_mean_en_df_1d").reshape(nsteps, -1),
                "upar": rd("i_parallel_flow_df_1d").reshape(nsteps, -1),
                "vexb": rd("i_poloidal_ExB_flow_df_1d").reshape(nsteps, -1),
            }
        psi = psi[: data["n"].shape[1]]
        # a restart rewrites the steps after its checkpoint: keep the last copy
        order = {}
        for i, g in enumerate(data["gstep"]):
            if max_step is None or g <= max_step:
                order[int(g)] = i
        idx = np.array([order[g] for g in sorted(order)])
        out = {k: v[idx] for k, v in data.items()}
        tpar = 2.0 * (out["epar"] - 0.5 * mass * out["upar"] ** 2 / E_CHARGE)
        out["T"] = (2.0 * out["tperp"] + tpar) / 3.0
        out["psi"] = psi
        return out

    od_a = read_oned(stage_a, args.f0_step)
    if args.f0_step not in set(od_a["gstep"].tolist()):
        emit({"error": f"stage A oneddiag has no gstep {args.f0_step}", "paths": [stage_a]})
        return 2
    i_t = int(np.where(od_a["gstep"] == args.f0_step)[0][-1])
    psi = od_a["psi"]
    band = (psi >= args.psi_range[0]) & (psi <= args.psi_range[1])
    target = {k: od_a[k][i_t] for k in ("n", "T", "upar", "vexb")}

    ods = {name: read_oned(path, None) for name, path in runs_in}
    common_last = min(int(od["gstep"][-1]) for od in ods.values())

    def rel(a, b):
        return (a - b) / b

    def rms(v):
        return float(np.sqrt(np.mean(v[band] ** 2)))

    profiles = {}
    for name, od in ods.items():
        i0 = 0
        i1 = int(np.where(od["gstep"] <= common_last)[0][-1])
        entry = {"first_gstep": int(od["gstep"][i0]), "last_gstep": int(od["gstep"][i1])}
        for tag, i in (("first", i0), ("last", i1)):
            entry[tag] = {
                "dn_over_n_target_rms": rms(rel(od["n"][i], target["n"])),
                "dn_over_n_target_max": float(np.max(np.abs(rel(od["n"][i], target["n"])[band]))),
                "dT_over_T_target_rms": rms(rel(od["T"][i], target["T"])),
                "dT_over_T_target_max": float(np.max(np.abs(rel(od["T"][i], target["T"])[band]))),
                "upar_rms_km_s": float(np.sqrt(np.mean(od["upar"][i][band] ** 2)) / 1e3),
                "vexb_rms_km_s": float(np.sqrt(np.mean(od["vexb"][i][band] ** 2)) / 1e3),
            }
        profiles[name] = entry
    od_r = ods[ref]
    i_r = int(np.where(od_r["gstep"] <= common_last)[0][-1])
    for name, od in ods.items():
        i1 = int(np.where(od["gstep"] <= common_last)[0][-1])
        profiles[name]["last_vs_ref"] = {
            "dn_over_n_rms": rms(rel(od["n"][i1], od_r["n"][i_r])),
            "dT_over_T_rms": rms(rel(od["T"][i1], od_r["T"][i_r])),
        }

    # --- particle weights -----------------------------------------------------
    # w = w0 * w1 is the marker's delta-f weight in physical particles; it is
    # the quantity whose rms sets the noise. w1 alone is kept for reference.
    x = xgc_reader.xgc1(path=stage_a)   # note: chdir into stage_a
    x.load_unitsm()
    x.setup_mesh()
    x.load_bfield()
    interp = LinearTriInterpolator(x.mesh.triobj, x.mesh.psi / x.psix)
    bmag = np.sqrt(x.bfield[0] ** 2 + x.bfield[1] ** 2 + x.bfield[2] ** 2)
    interp_b = LinearTriInterpolator(x.mesh.triobj, bmag)
    edges = np.linspace(0.0, 1.0, args.nbins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    e_edges = [0, 1, 2, 4, 6, 9, 12, 16, 25, 100]

    def read_particles(path):
        with adios2.FileReader(path) as fh:
            ph = np.asarray(fh.read("iphase"))
            gid = np.asarray(fh.read("igid")).ravel()
        return ph, gid

    def rms_(v):
        return float(np.sqrt(np.mean(v ** 2))) if v.size else None

    weights = {}
    snapshots = {}
    w0_ref = None
    for name, path in [("maxwell", stage_a)] + runs_in:
        limit = args.f0_step if name == "maxwell" else common_last
        files = particle_files(path, limit)
        if not files:
            continue
        series = {"gstep": [], "w_rms": [], "w_mean": [], "w_p99": [], "w1_rms": []}
        first = last = None
        for step, pf in files:
            ph, gid = read_particles(pf)
            w = ph[:, 4] * ph[:, 7]
            series["gstep"].append(step)
            series["w_rms"].append(rms_(w))
            series["w_mean"].append(float(np.mean(w)))
            series["w_p99"].append(float(np.percentile(np.abs(w), 99)))
            series["w1_rms"].append(rms_(ph[:, 4]))
            if first is None:
                first = (step, ph, gid)
            last = (step, ph, gid)
        entry = {"series": series}
        for tag, (step, ph, gid) in (("first", first), ("last", last)):
            w1 = ph[:, 4]
            w0 = ph[:, 7]
            w = w1 * w0
            if w0_ref is None:
                w0_ref = float(np.mean(w0))
            p = np.asarray(interp(ph[:, 0], ph[:, 1]).filled(np.nan))
            b = np.asarray(interp_b(ph[:, 0], ph[:, 1]).filled(np.nan))
            vpar = E_CHARGE * ph[:, 3] * b / mass
            e_over_t = (0.5 * mass * vpar ** 2 + ph[:, 6] * b) / E_CHARGE / np.interp(p, psi, target["T"])
            prof = [rms_(w[(p >= lo) & (p < hi)]) for lo, hi in zip(edges[:-1], edges[1:])]
            by_e = []
            for lo, hi in zip(e_edges[:-1], e_edges[1:]):
                k = (e_over_t >= lo) & (e_over_t < hi)
                if k.any():
                    by_e.append({"E_over_T": [lo, hi], "n": int(k.sum()),
                                 "w_rms": rms_(w[k]), "w_mean": float(np.mean(w[k])),
                                 "share_of_w_sq": float(np.sum(w[k] ** 2) / np.sum(w ** 2))})
            entry[tag] = {
                "gstep": int(step),
                "n_markers": int(w1.size),
                "w_rms": rms_(w),
                "w_rms_over_w0_mean": rms_(w) / float(np.mean(w0)),
                "w_mean": float(np.mean(w)),
                "w1_rms": rms_(w1),
                "w0_mean": float(np.mean(w0)),
                "w0_cv": float(np.std(w0) / np.mean(w0)),
                "w_rms_vs_psi": prof,
                "w_by_energy": by_e,
            }
            snapshots.setdefault(name, {})[tag] = (w, w0)
        # the same markers at the first and last snapshot
        _, ph0, g0 = first
        _, ph1, g1 = last
        common, i0, i1 = np.intersect1d(g0, g1, return_indices=True)
        if common.size:
            dw = ph1[i1, 4] * ph1[i1, 7] - ph0[i0, 4] * ph0[i0, 7]
            entry["tracked"] = {"n_common": int(common.size), "dw_rms": rms_(dw),
                                "dw_mean": float(np.mean(dw))}
        weights[name] = entry

    # --- figures ------------------------------------------------------------------
    names = [n for n, _ in runs_in]
    png = {}

    fig, ax = plt.subplots(2, 2, figsize=(13, 9), sharex=True)
    for name in names:
        od = ods[name]
        i1 = int(np.where(od["gstep"] <= common_last)[0][-1])
        line, = ax[0, 0].plot(psi, rel(od["n"][0], target["n"]) * 100, "--", lw=1)
        ax[0, 0].plot(psi, rel(od["n"][i1], target["n"]) * 100, color=line.get_color(), lw=1.8, label=name)
        ax[0, 1].plot(psi, rel(od["T"][0], target["T"]) * 100, "--", color=line.get_color(), lw=1)
        ax[0, 1].plot(psi, rel(od["T"][i1], target["T"]) * 100, color=line.get_color(), lw=1.8)
        ax[1, 0].plot(psi, od["upar"][i1] / 1e3, color=line.get_color(), lw=1.8)
        ax[1, 1].plot(psi, od["vexb"][i1] / 1e3, color=line.get_color(), lw=1.8)
    ax[1, 0].plot(psi, target["upar"] / 1e3, "k:", lw=1.5, label=f"stage A @ {args.f0_step}")
    ax[1, 1].plot(psi, target["vexb"] / 1e3, "k:", lw=1.5)
    ax[0, 0].set_ylabel("(n - n_target)/n_target [%]")
    ax[0, 1].set_ylabel("(T - T_target)/T_target [%]")
    ax[1, 0].set_ylabel("u_par [km/s]")
    ax[1, 1].set_ylabel("poloidal ExB flow [km/s]")
    for a in ax[1]:
        a.set_xlabel("psi_N")
    for a in ax.ravel():
        a.axhline(0, color="gray", lw=0.5)
    ax[0, 0].legend(fontsize=8)
    ax[1, 0].legend(fontsize=8)
    fig.suptitle(f"dashed: first record, solid: gstep {common_last}; target = stage A gstep {args.f0_step}")
    fig.tight_layout()
    png["profiles"] = f"{out_prefix}_profiles.png"
    fig.savefig(png["profiles"], dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(1, 2, figsize=(13, 4.5))
    for name in names:
        od = ods[name]
        dn = np.sqrt(np.mean(rel(od["n"], target["n"][None, :])[:, band] ** 2, axis=1))
        dT = np.sqrt(np.mean(rel(od["T"], target["T"][None, :])[:, band] ** 2, axis=1))
        ax[0].plot(od["gstep"], dn * 100, label=name)
        ax[1].plot(od["gstep"], dT * 100, label=name)
    ax[0].set_ylabel("rms (n - n_target)/n_target [%]")
    ax[1].set_ylabel("rms (T - T_target)/T_target [%]")
    for a in ax:
        a.set_xlabel("gstep")
    ax[0].legend(fontsize=8)
    fig.tight_layout()
    png["profile_drift"] = f"{out_prefix}_profile_drift.png"
    fig.savefig(png["profile_drift"], dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(2, 2, figsize=(13, 9))
    for name, entry in weights.items():
        s_ = entry["series"]
        style = ":" if name == "maxwell" else "-"
        line, = ax[0, 0].plot(s_["gstep"], np.array(s_["w_rms"]) / w0_ref, style, label=name)
        ax[0, 1].plot(centers, [np.nan if v is None else v / w0_ref for v in entry["last"]["w_rms_vs_psi"]],
                      style, color=line.get_color(), label=f"{name} @ {entry['last']['gstep']}")
        lo = [b_["E_over_T"][0] for b_ in entry["last"]["w_by_energy"]]
        ax[1, 0].semilogy([0.5 * (b_["E_over_T"][0] + min(b_["E_over_T"][1], 30)) for b_ in entry["last"]["w_by_energy"]],
                          [b_["w_rms"] / w0_ref for b_ in entry["last"]["w_by_energy"]],
                          "o" + style, color=line.get_color(), label=f"{name} @ {entry['last']['gstep']}")
        w_last = snapshots[name]["last"][0] / w0_ref
        ax[1, 1].hist(w_last, bins=np.linspace(-2, 2, 201), histtype="step", color=line.get_color(),
                      density=True, label=name)
    ax[0, 0].set_xlabel("gstep")
    ax[0, 0].set_ylabel("rms(w0 w1) / <w0>")
    ax[0, 0].set_yscale("log")
    ax[0, 0].legend(fontsize=8)
    ax[0, 1].set_xlabel("psi_N")
    ax[0, 1].set_ylabel("rms(w0 w1) / <w0> (last snapshot)")
    ax[0, 1].set_yscale("log")
    ax[0, 1].legend(fontsize=8)
    ax[1, 0].set_xlabel("E / T_target (bin centre)")
    ax[1, 0].set_ylabel("rms(w0 w1) / <w0> per energy bin (last)")
    ax[1, 0].legend(fontsize=8)
    ax[1, 1].set_xlabel("w0 w1 / <w0> (last snapshot)")
    ax[1, 1].set_yscale("log")
    fig.suptitle(f"<w0> = {w0_ref:.3e} physical particles per marker (identical marker loading in all runs)")
    fig.tight_layout()
    png["weights"] = f"{out_prefix}_weights.png"
    fig.savefig(png["weights"], dpi=120)
    plt.close(fig)

    result = {
        "f0_step": args.f0_step,
        "ref": ref,
        "common_last_gstep": common_last,
        "psi_range": list(args.psi_range),
        "profiles": profiles,
        "weights": weights,
        "psi_bins": centers.tolist(),
    }
    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    summary = {
        name: {
            "first": profiles[name]["first"],
            "last": profiles[name]["last"],
            "last_vs_ref": profiles[name]["last_vs_ref"],
            "w_rms_first_last": ([weights[name]["first"]["w_rms"], weights[name]["last"]["w_rms"]]
                                 if name in weights else None),
            "w_rms_over_w0_first_last": ([weights[name]["first"]["w_rms_over_w0_mean"],
                                          weights[name]["last"]["w_rms_over_w0_mean"]]
                                         if name in weights else None),
            "w0_cv": weights[name]["first"]["w0_cv"] if name in weights else None,
            "tracked": weights.get(name, {}).get("tracked"),
        }
        for name in names
    }
    if "maxwell" in weights:
        summary["maxwell"] = {
            "w_rms_first_last": [weights["maxwell"]["first"]["w_rms"], weights["maxwell"]["last"]["w_rms"]],
            "w_rms_over_w0_first_last": [weights["maxwell"]["first"]["w_rms_over_w0_mean"],
                                         weights["maxwell"]["last"]["w_rms_over_w0_mean"]],
            "w0_cv": weights["maxwell"]["first"]["w0_cv"],
            "tracked": weights["maxwell"].get("tracked"),
        }
    emit({"json": json_path, "png": png, "common_last_gstep": common_last, "summary": summary})
    return 0


if __name__ == "__main__":
    sys.exit(main())
