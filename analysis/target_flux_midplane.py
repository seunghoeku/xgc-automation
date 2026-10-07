#!/usr/bin/env python3
"""Target heat/particle flux mapped to the outboard midplane and expressed per unit psi.

usage: python target_flux_midplane.py <out_png> --steps 180,190,200 [--psimax 1.1] "<label>=<run_dir>" ...
Per run (local copies): xgc.mesh.bp, xgc.equil.bp, xgc.bfield.bp (first run), xgc.f2d.<step>.bp, xgc.heatdiag2.bp.
Panels per target (inner / outer rows):
  (1) Q_par at the wall nodes (f2d, along B) vs r_mid - r_sep, the outboard-midplane distance of the flux surface;
  (2) Q_par * B_mid/B_target (flux-tube mapped parallel heat flux at the midplane) vs r_mid - r_sep;
  (3) dP/dpsi_N from heatdiag2 (power to the plate per unit normalised flux, ions and electrons) vs psi_N;
  (4) dP/dr_mid (same power per mm of midplane radius) vs r_mid - r_sep.
"""
import sys, os, json
import numpy as np, adios2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.tri import Triangulation, LinearTriInterpolator

argv = sys.argv[1:]
def popopt(name, default, cast):
    if name in argv:
        k = argv.index(name); v = argv[k + 1]; del argv[k:k + 2]; return cast(v)
    return default
STEPS = popopt("--steps", (180, 190, 200), lambda v: tuple(int(x) for x in v.split(",")))
PSIMAX = popopt("--psimax", 1.1, float)
out = argv[0]; RUNS = [a.split("=", 1) for a in argv[1:]]
COLORS = ["tab:blue", "tab:red", "tab:orange", "tab:green"]
def rd(p, v, st=None):
    with adios2.FileReader(p) as f: return np.squeeze(np.asarray(f.read(v) if st is None else f.read(v, step_selection=[st, 1])))

d0 = RUNS[0][1]
rz = rd(d0 + "/xgc.mesh.bp", "rz"); r, z = rz[:, 0], rz[:, 1]; cnct = rd(d0 + "/xgc.mesh.bp", "nd_connect_list")
psi = rd(d0 + "/xgc.mesh.bp", "psi"); wn = rd(d0 + "/xgc.mesh.bp", "grid_wall_nodes") - 1
eq = d0 + "/xgc.equil.bp"; x_psi = float(rd(eq, "eq_x_psi")); x_r = float(rd(eq, "eq_x_r")); x_z = float(rd(eq, "eq_x_z"))
ax_r = float(rd(eq, "eq_axis_r")); ax_z = float(rd(eq, "eq_axis_z")); psin = psi / x_psi
B = rd(d0 + "/xgc.bfield.bp", "bfield"); Bmag = np.sqrt((B**2).sum(0))
tri = Triangulation(r, z, cnct); ipsi = LinearTriInterpolator(tri, psin); iB = LinearTriInterpolator(tri, Bmag)
# outboard midplane map psi_N(R) at Z = Z_axis
Rline = np.linspace(ax_r, r.max(), 3000); pl = np.array(ipsi(Rline, np.full_like(Rline, ax_z))); ok = np.isfinite(pl)
Rline, pl = Rline[ok], pl[ok]; Bl = np.array(iB(Rline, np.full_like(Rline, ax_z)))
# keep the monotonic part up to the first maximum
imax = np.argmax(pl); Rline, pl, Bl = Rline[:imax + 1], pl[:imax + 1], Bl[:imax + 1]
R_sep = np.interp(1.0, pl, Rline)
def rmid(psin_v): return (np.interp(psin_v, pl, Rline) - R_sep) * 1e3   # mm
def Bmid(psin_v): return np.interp(psin_v, pl, Bl)
print(f"outboard midplane: R_sep = {R_sep:.4f} m, psi_N {PSIMAX} -> r_mid-r_sep = {rmid(PSIMAX):.1f} mm, B_mid(sep) = {Bmid(1.0):.2f} T")

div = z[wn] < x_z
tw = {"inner": np.where(div & (r[wn] < x_r))[0], "outer": np.where(div & (r[wn] >= x_r))[0]}
for k in tw: tw[k] = tw[k][np.argsort(psin[wn][tw[k]])]
hp = d0 + "/xgc.heatdiag2.bp"; hr = rd(hp, "r", 0); hz = rd(hp, "z", 0); hpsin = rd(hp, "psi", 0) / x_psi; ds = rd(hp, "ds", 0)
hdiv = hz < x_z
ts = {"inner": np.where(hdiv & (hr < x_r))[0], "outer": np.where(hdiv & (hr >= x_r))[0]}
for k in ts: ts[k] = ts[k][np.argsort(hpsin[ts[k]])]

def load(d):
    o = {}
    st = [s for s in STEPS if os.path.exists(f"{d}/xgc.f2d.{s:05d}.bp")]
    for sp in ("e", "i"):
        o[sp + "_Q"] = np.mean([(rd(f"{d}/xgc.f2d.{s:05d}.bp", sp + "_par_en_flux_df") + rd(f"{d}/xgc.f2d.{s:05d}.bp", sp + "_par_en_flux_f0"))[wn] for s in st], 0)
    p = d + "/xgc.heatdiag2.bp"
    with adios2.FileReader(p) as f: v = f.available_variables()
    sv = "gstep" if "gstep" in v else "step"; n = int(v[sv]["AvailableStepsCount"])
    hs = [int(rd(p, sv, i)) for i in range(n)]; idx = [hs.index(s) for s in STEPS if s in hs]
    t = [float(rd(p, "time", i)) for i in range(2)]; dt = t[1] - t[0]
    for sp in ("e", "i"):
        o[sp + "_P"] = np.mean([rd(p, sp + "_para_energy", i)[1:] + rd(p, sp + "_perp_energy", i)[1:] for i in idx], 0) / dt   # W per segment
    o["f2d_steps"] = st
    return o
data = {lbl: load(d) for lbl, d in RUNS}
Bw = Bmag[wn]

plt.rcParams.update({"font.size": 13, "axes.titlesize": 13, "axes.labelsize": 13, "legend.fontsize": 11, "lines.linewidth": 2.0, "lines.markersize": 5})
fig, axs = plt.subplots(2, 4, figsize=(28, 12))
summary = {}
for i, tg in enumerate(("inner", "outer")):
    idx = tw[tg]; idx = idx[(psin[wn][idx] <= PSIMAX)]
    xw = rmid(psin[wn][idx]); fac = Bmid(psin[wn][idx]) / Bw[idx]
    seg = ts[tg]; seg = seg[hpsin[seg] <= PSIMAX]
    # psi width of each segment from neighbouring segment midpoints
    ps = hpsin[seg]; dpsi = np.gradient(ps); dpsi = np.where(dpsi > 0, dpsi, np.nan)
    xs = rmid(ps); drm = np.gradient(xs); drm = np.where(drm > 0, drm, np.nan)
    for (lbl, _), c in zip(RUNS, COLORS):
        D = data[lbl]; Q = np.abs(D["i_Q"][idx] + D["e_Q"][idx]) / 1e6
        axs[i, 0].plot(xw, Q, "-o", ms=3, color=c, label=lbl)
        axs[i, 1].plot(xw, Q * fac, "-o", ms=3, color=c, label=lbl)
        Pi = D["i_P"][seg]; Pe = D["e_P"][seg]
        axs[i, 2].plot(ps, Pi / dpsi / 1e6, "-", color=c, label=f"{lbl} ions"); axs[i, 2].plot(ps, Pe / dpsi / 1e6, "--", color=c, label=f"{lbl} electrons")
        axs[i, 3].plot(xs, (Pi + Pe) / drm / 1e6, "-", color=c, label=lbl)
        summary.setdefault(lbl, {})[tg] = {"P_i_MW": float(np.nansum(Pi)) / 1e6, "P_e_MW": float(np.nansum(Pe)) / 1e6,
            "lambda_fit_note": "see figure", "Qpar_peak_MWm2": float(np.nanmax(Q)), "Qpar_mid_peak_MWm2": float(np.nanmax(Q * fac))}
    axs[i, 0].set_ylabel(r"$|Q_\parallel|$ at the wall node (MW/m$^2$)"); axs[i, 0].set_title(f"{tg} target: parallel heat flux at the plate", fontsize=13)
    axs[i, 1].set_ylabel(r"$|Q_\parallel|\,B_{mid}/B_{target}$ (MW/m$^2$)"); axs[i, 1].set_title(f"{tg} target: flux-tube mapped to the outboard midplane", fontsize=13)
    axs[i, 2].set_ylabel(r"$dP/d\psi_N$ (MW per unit $\psi_N$)"); axs[i, 2].set_title(f"{tg} target: power per unit psi (heatdiag)", fontsize=13); axs[i, 2].set_xlabel(r"$\psi_N$"); axs[i, 2].axvline(1, color="k", ls=":")
    axs[i, 3].set_ylabel(r"$dP/dr_{mid}$ (MW/mm, i+e)"); axs[i, 3].set_title(f"{tg} target: power per mm of midplane radius", fontsize=13)
    for a in (axs[i, 0], axs[i, 1], axs[i, 3]): a.set_xlabel(r"$r_{mid}-r_{sep}$ (mm, outboard midplane)"); a.axvline(0, color="k", ls=":")
    for a in axs[i]: a.grid(alpha=0.3)
    axs[i, 2].set_xlim(0.975, PSIMAX)
axs[0, 0].legend(fontsize=11); axs[0, 2].legend(fontsize=9)
fig.suptitle(f"Target fluxes mapped to the outboard midplane, gstep {STEPS} average, psi_N <= {PSIMAX}")
fig.tight_layout(); fig.savefig(out, dpi=150); plt.close(fig)
# per-target copies with larger panels
for i, tg in enumerate(("inner", "outer")):
    f2, a2 = plt.subplots(1, 4, figsize=(28, 7.5))
    for j in range(4):
        src = axs[i, j]
        for ln in src.get_lines():
            a2[j].plot(ln.get_xdata(), ln.get_ydata(), color=ln.get_color(), ls=ln.get_linestyle(), marker=ln.get_marker(), label=ln.get_label(), lw=2.2, ms=5)
        a2[j].set_title(src.get_title()); a2[j].set_xlabel(src.get_xlabel()); a2[j].set_ylabel(src.get_ylabel()); a2[j].grid(alpha=0.3)
        a2[j].set_xlim(src.get_xlim()); a2[j].set_ylim(src.get_ylim())
    a2[0].legend(fontsize=11); a2[2].legend(fontsize=9)
    f2.suptitle(fig._suptitle.get_text() + f" -- {tg} target"); f2.tight_layout(); f2.savefig(out.replace(".png", f"_{tg}.png"), dpi=150); plt.close(f2)

json.dump({"R_sep_mid_m": R_sep, "psimax": PSIMAX, "rmid_at_psimax_mm": float(rmid(PSIMAX)), "runs": summary}, open(os.path.splitext(out)[0] + ".json", "w"), indent=1)
print("wrote", out); print(json.dumps(summary, indent=1))
