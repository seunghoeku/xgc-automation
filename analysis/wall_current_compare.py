"""Per-node wall currents, wall potential and target fluxes along the outer plate for conducting-sheath runs.

usage: python wall_current_compare.py <out_png> --steps 110,120,130 "label=dir" ...
Currents come from xgc.sheathdiag (lost charge per diag interval / interval), potentials from xgc.2d,
particle fluxes from xgc.f2d (wall nodes).
"""
import sys, os, glob, re
import numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 12, "lines.linewidth": 2})
argv = sys.argv[1:]
steps = (110, 120, 130)
if "--steps" in argv:
    k = argv.index("--steps"); steps = tuple(int(x) for x in argv[k + 1].split(",")); del argv[k:k + 2]
out = argv[0]; runs = [a.split("=", 1) for a in argv[1:]]

def rd(f, k, i=None):
    return np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)

fig, axs = plt.subplots(2, 3, figsize=(22, 11))
summary = {}
for lab, d in runs:
    with adios2.FileReader(f"{d}/xgc.mesh.bp") as f:
        rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1
    with adios2.FileReader(f"{d}/xgc.equil.bp") as f:
        xpsi = float(f.read("eq_x_psi")); xr = float(f.read("eq_x_r")); xz = float(f.read("eq_x_z"))
    psin = psi / xpsi; R = rz[:, 0]; Z = rz[:, 1]
    pw = psin[wall]; Rw = R[wall]; Zw = Z[wall]
    outer = (Rw > xr) & (Zw < xz); inner = (Rw < xr) & (Zw < xz); main = Zw >= xz
    jo = np.where(outer & (pw > 0.975) & (pw < 1.1))[0]; jo = jo[np.argsort(pw[jo])]
    # sheathdiag currents
    with adios2.FileReader(f"{d}/xgc.sheathdiag.bp") as f:
        ns = int(f.available_variables()["gstep"]["AvailableStepsCount"])
        g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)]); t = np.array([float(rd(f, "time", i)[0]) for i in range(ns)])
        lost = np.array([rd(f, "sheath_lost", i) for i in range(ns)]); ilost = np.array([rd(f, "sheath_ilost", i) for i in range(ns)])
        shp = np.array([rd(f, "sheath_pot", i) for i in range(ns)])
    # sheath mode 3 (all runs compared here): XGC adds ilost into lost before writing (sheath_adjust.cpp add_y_to_x),
    # so sheath_lost = net charge lost (e + i) and the electron part is lost - ilost. (Mode-1 files hold electrons only.)
    dt = np.diff(t, prepend=t[0] - (t[1] - t[0])); In = lost / dt[:, None]; Ii = ilost / dt[:, None]; Ie = In - Ii
    sel = np.isin(g, steps)
    Iem = Ie[sel].mean(0); Iim = Ii[sel].mean(0); Inm = In[sel].mean(0); shpm = shp[sel].mean(0)
    # potentials and fluxes
    pots = []; Gi = []; Ge = []
    for s in steps:
        with adios2.FileReader(f"{d}/xgc.2d.{s:05d}.bp") as f:
            pots.append((rd(f, "pot0") + rd(f, "dpot"))[wall])
        if not os.path.isdir(f"{d}/xgc.f2d.{s:05d}.bp"):
            print(f"  {lab}: xgc.f2d.{s:05d}.bp missing, flux panels average the remaining steps"); continue
        with adios2.FileReader(f"{d}/xgc.f2d.{s:05d}.bp") as f:
            ni = rd(f, "i_den"); ne = rd(f, "e_den")
            ui = rd(f, "i_parallel_flow_df") + rd(f, "i_parallel_flow_f0"); ue = rd(f, "e_parallel_flow_df") + rd(f, "e_parallel_flow_f0")
            Gi.append(np.abs(ni * ui)[wall]); Ge.append(np.abs(ne * ue)[wall])
    pot = np.mean(pots, 0); Gi = np.mean(Gi, 0); Ge = np.mean(Ge, 0)
    l, = axs[0, 0].plot(pw[jo], pot[jo], "-o", ms=4, label=lab)
    axs[0, 0].plot(pw[jo], shpm[jo], "--", color=l.get_color(), lw=1.2)
    axs[0, 1].plot(pw[jo], Inm[jo], "-o", ms=4, color=l.get_color(), label=lab)
    axs[0, 2].plot(pw[jo], Iem[jo], "-", color=l.get_color(), label=f"{lab} I_e"); axs[0, 2].plot(pw[jo], Iim[jo], "--", color=l.get_color(), label=f"{lab} I_i")
    axs[1, 0].plot(pw[jo], Gi[jo], "-o", ms=4, color=l.get_color(), label=lab)
    axs[1, 1].plot(pw[jo], Ge[jo], "-o", ms=4, color=l.get_color(), label=lab)
    axs[1, 2].plot(g, In.sum(1), "-", color=l.get_color(), label=f"{lab} total"); axs[1, 2].plot(g, In[:, inner | outer].sum(1), "--", color=l.get_color(), label=f"{lab} targets")
    summary[lab] = dict(I_net_total_A=float(In[sel].sum(1).mean()), I_net_targets_A=float(In[sel][:, inner | outer].sum(1).mean()),
                        I_net_main_A=float(In[sel][:, main].sum(1).mean()), I_e_main_A=float(Ie[sel][:, main].sum(1).mean()), I_i_main_A=float(Ii[sel][:, main].sum(1).mean()),
                        I_e_targets_A=float(Ie[sel][:, inner | outer].sum(1).mean()), I_i_targets_A=float(Ii[sel][:, inner | outer].sum(1).mean()), sum_abs_Inode_outer_A=float(np.abs(Inm[outer]).sum()),
                        I_net_outer_A=float(Inm[outer].sum()), pot_outer_min_V=float(pot[jo].min()), pot_outer_max_V=float(pot[jo].max()),
                        Gi_min_dip=float(Gi[jo][(pw[jo] > 1.01) & (pw[jo] < 1.03)].min()) if np.any((pw[jo] > 1.01) & (pw[jo] < 1.03)) else None)
axs[0, 0].set_title("outer target: wall potential pot0+dpot (solid), sheath_pot (dashed)"); axs[0, 0].set_ylabel("V")
axs[0, 1].set_title("outer target: net current per wall node (e+i)"); axs[0, 1].set_ylabel("A")
axs[0, 2].set_title("outer target: electron (solid) / ion (dashed) current per node"); axs[0, 2].set_ylabel("A")
axs[1, 0].set_title(r"outer target: $|\Gamma_{\parallel,i}|$ at wall nodes (f2d)"); axs[1, 0].set_ylabel(r"m$^{-2}$s$^{-1}$")
axs[1, 1].set_title(r"outer target: $|\Gamma_{\parallel,e}|$ at wall nodes (f2d)"); axs[1, 1].set_ylabel(r"m$^{-2}$s$^{-1}$")
axs[1, 2].set_title("net wall current vs time (solid total, dashed targets)"); axs[1, 2].set_xlabel("gstep"); axs[1, 2].set_ylabel("A"); axs[1, 2].set_ylim(-1500, 500)
for a in axs.flat[:5]:
    a.axvline(1, color="k", ls=":"); a.set_xlabel(r"$\psi_N$ along the outer target"); a.grid(alpha=.3)
for a in (axs[0, 1], axs[0, 2], axs[1, 2]): a.axhline(0, color="k", lw=0.8)
axs[1, 2].grid(alpha=.3)
axs[0, 0].legend(fontsize=9); axs[0, 2].legend(fontsize=8); axs[1, 2].legend(fontsize=8)
fig.suptitle(f"Conducting-sheath runs, gstep {steps} average (currents from sheathdiag: net = sheath_lost, ion = sheath_ilost, electron = difference)"); fig.tight_layout(); fig.savefig(out, dpi=140)
import json; json.dump(summary, open(out.replace(".png", ".json"), "w"), indent=1)
print(json.dumps(summary, indent=1)); print("wrote", out)
