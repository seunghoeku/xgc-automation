#!/usr/bin/env python3
"""Login-node step: poloidal structure of the f_init moments on one flux surface.

For the unsmoothed f_init and one or more smoothed ones, the local parallel flow and
temperature of the full distribution (f_M + f_g) are computed node by node
with ``xgc_reader.distribution.core.get_local_moments`` and drawn against
the geometric poloidal angle theta = atan2(Z - Z_axis, R - R_axis) on the
mesh flux surface nearest ``--psi``: flow on the left axis, temperature on
the right. The low-m content is quantified by Fourier coefficients in theta
(trapezoidal rule on the sorted, periodic node angles). Last stdout line:
``FINIT_JSON {...}``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def emit(payload: dict) -> None:
    print("FINIT_JSON " + json.dumps(payload), flush=True)


def harmonics(theta, q, m_max):
    """Complex Fourier coefficients c_m = (1/2pi) int q e^{-i m theta} dtheta, m = 0..m_max."""
    import numpy as np
    th = np.concatenate([theta, theta[:1] + 2 * np.pi])
    qq = np.concatenate([q, q[:1]])
    return np.array([np.trapz(qq * np.exp(-1j * m * th), th) / (2 * np.pi) for m in range(m_max + 1)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xgc-reader", required=True)
    parser.add_argument("--run-dir", required=True, help="stage A directory (mesh, units)")
    parser.add_argument("--no-smooth", required=True)
    parser.add_argument("--smooth", action="append", required=True, help="smoothed f_init (repeatable)")
    parser.add_argument("--smooth-label", action="append", default=[], help="legend label per --smooth")
    parser.add_argument("--psi", type=float, default=0.45)
    parser.add_argument("--m-max", type=int, default=8)
    parser.add_argument("--m-low", type=int, default=1, help="harmonics up to this m count as low-m")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--out-prefix", required=True)
    args = parser.parse_args()

    sys.path.insert(0, args.xgc_reader)
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from xgc_reader import base as xgc_reader
    from xgc_reader.distribution import core as xd

    out_prefix = os.path.abspath(args.out_prefix)
    os.makedirs(os.path.dirname(out_prefix), exist_ok=True)
    files = [os.path.abspath(args.no_smooth)] + [os.path.abspath(p) for p in args.smooth]
    for path in files:
        if not os.path.exists(path):
            emit({"error": "missing", "paths": [path]})
            return 2

    x = xgc_reader.xgc1(path=os.path.abspath(args.run_dir))   # chdir
    x.load_unitsm()
    x.setup_mesh()
    psi_surf = np.asarray(x.mesh.psi_surf) / x.psix
    closed = np.where(psi_surf < 1.0)[0]
    isurf = int(closed[np.argmin(np.abs(psi_surf[closed] - args.psi))])
    nsurf = int(x.mesh.surf_len[isurf])
    nodes = np.asarray(x.mesh.surf_idx[isurf, :nsurf]) - 1
    r_ax = float(x.eq_axis_r)
    z_ax = float(getattr(x, "eq_axis_z", 0.0))
    theta = np.arctan2(np.asarray(x.mesh.z)[nodes] - z_ax, np.asarray(x.mesh.r)[nodes] - r_ax)
    order = np.argsort(theta)
    nodes, theta = nodes[order], theta[order]

    labels = ["No smoothing"] + [(args.smooth_label[k] if k < len(args.smooth_label) else f"smoothed {k + 1}")
                                 for k in range(len(args.smooth))]
    rows = []
    for path, label in zip(files, labels):
        dist = xd.XGCDistribution.from_xgc_input(path)
        den, temp_ev, flow = xd.get_local_moments(dist, x)
        t = np.asarray(temp_ev)[nodes]
        u = np.asarray(flow)[nodes] * 1e-3                    # km/s
        ct, cu = harmonics(theta, t, args.m_max), harmonics(theta, u, args.m_max)
        rows.append({"label": label, "file": path, "T": t, "u": u, "cT": ct, "cu": cu})

    # amplitude of harmonic m of a real signal: |c_0| for m = 0, 2|c_m| otherwise
    def amp(c):
        a = 2 * np.abs(c)
        a[0] = np.abs(c[0])
        return a

    font, figsize, dpi, lw = (22, (13, 7.5), 180, 2.6) if args.paper else (14, (10, 6), 150, 2.0)
    plt.rcParams.update({"font.size": font})
    fig, ax_u = plt.subplots(figsize=figsize)
    ax_t = ax_u.twinx()
    cu_col, ct_col = "C0", "C3"
    for row, style in zip(rows, ("-", "--", ":", "-.")):
        ax_u.plot(theta, row["u"], style, color=cu_col, lw=lw, label=f"$u_\\parallel$, {row['label']}")
        ax_t.plot(theta, row["T"], style, color=ct_col, lw=lw, label=f"$T$, {row['label']}")
    ax_u.set_xlabel("Poloidal angle $\\theta$ (rad)")
    ax_u.set_ylabel("$u_\\parallel$ (km/s)", color=cu_col)
    ax_t.set_ylabel("$T$ (eV)", color=ct_col)
    ax_u.tick_params(axis="y", colors=cu_col)
    ax_t.tick_params(axis="y", colors=ct_col)
    ax_u.set_xlim(-np.pi, np.pi)
    ax_u.set_xticks([-np.pi, -np.pi / 2, 0, np.pi / 2, np.pi])
    ax_u.set_xticklabels(["$-\\pi$", "$-\\pi/2$", "0", "$\\pi/2$", "$\\pi$"])
    handles = ax_u.get_legend_handles_labels()[0] + ax_t.get_legend_handles_labels()[0]
    names = ax_u.get_legend_handles_labels()[1] + ax_t.get_legend_handles_labels()[1]
    ax_u.legend(handles, names, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=2,
                fontsize=int(font * 0.75), frameon=False)
    ax_u.set_title(f"$\\psi_N = {psi_surf[isurf]:.2f}$, {nsurf} nodes")
    fig.tight_layout()
    png = f"{out_prefix}.png"
    fig.savefig(png, dpi=dpi, bbox_inches="tight")
    plt.close(fig)

    summary = []
    for row in rows:
        at, au = amp(row["cT"]), amp(row["cu"])
        hi = slice(args.m_low + 1, None)
        summary.append({
            "label": row["label"],
            "T_mean_ev": float(at[0]), "u_mean_kms": float(au[0]),
            "T_amp_ev": at.tolist(), "u_amp_kms": au.tolist(),
            "T_phase": np.angle(row["cT"]).tolist(), "u_phase": np.angle(row["cu"]).tolist(),
            "T_rms_ev": float(np.std(row["T"])), "u_rms_kms": float(np.std(row["u"])),
            "T_high_m_ev": float(np.sqrt(np.sum(at[hi] ** 2) / 2)),
            "u_high_m_kms": float(np.sqrt(np.sum(au[hi] ** 2) / 2)),
        })
    change = {}
    for s in summary[1:]:
        change[s["label"]] = {}
        for q, key in (("T", "T_amp_ev"), ("u", "u_amp_kms")):
            a0, a1 = np.array(summary[0][key]), np.array(s[key])
            change[s["label"]][q] = {f"m{m}": (float(a1[m] / a0[m] - 1) if a0[m] > 0 else None)
                                     for m in range(args.m_low + 1)}

    json_path = f"{out_prefix}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"psi_n": float(psi_surf[isurf]), "surface": isurf, "nodes": nsurf,
                   "theta": theta.tolist(), "summary": summary, "relative_change_smoothed": change,
                   "series": [{"label": r["label"], "T_ev": r["T"].tolist(), "u_kms": r["u"].tolist()}
                              for r in rows]}, fh)
    emit({"json": json_path, "png": {"moments": png}, "psi_n": float(psi_surf[isurf]), "nodes": nsurf,
          "summary": [{k: (v[: args.m_low + 1] if isinstance(v, list) else v)
                       for k, v in s.items() if "phase" not in k} for s in summary],
          "relative_change_smoothed": change})
    return 0


if __name__ == "__main__":
    sys.exit(main())
