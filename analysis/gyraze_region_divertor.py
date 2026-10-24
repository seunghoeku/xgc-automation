"""GYRAZE surrogate region (converged / unconverged-projected / fallback) at the divertor wall nodes vs the sheath potential.
usage: python gyraze_region_divertor.py <run_dir> <out_png> <w1_lo,w1_hi> <w2_lo,w2_hi> <f2d_step_for_Te>"""
import sys, numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 11})
d, out = sys.argv[1], sys.argv[2]
W = [tuple(int(x) for x in a.split(",")) for a in sys.argv[3:5]]; fstep = int(sys.argv[5])
rd = lambda f, k, i=None: np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)
with adios2.FileReader(f"{d}/xgc.mesh.bp") as f: rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1
with adios2.FileReader(f"{d}/xgc.equil.bp") as f: xpsi = float(rd(f, "eq_x_psi")[0]); xr = float(rd(f, "eq_x_r")[0]); xz = float(rd(f, "eq_x_z")[0])
with adios2.FileReader(f"{d}/xgc.gyraze.bp") as f: ang = np.degrees(rd(f, "bfield_impact_angle"))
with adios2.FileReader(f"{d}/xgc.f2d.{fstep:05d}.bp") as f: Te = ((rd(f, "e_T_para") + 2 * rd(f, "e_T_perp")) / 3)[wall]
pw, Rw, Zw = psi[wall] / xpsi, rz[wall, 0], rz[wall, 1]
div = (Zw < xz) & (pw >= 1.0)   # divertor plates, SOL side only (private-flux-side nodes, alpha 0-0.9 deg, excluded); outer = div & (Rw > xr); inner = div & (Rw < xr)
outer = div & (Rw > xr); inner = div & (Rw < xr)
with adios2.FileReader(f"{d}/xgc.sheathdiag.bp") as f:
    ns = int(f.available_variables()["gstep"]["AvailableStepsCount"])
    g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)])
    C = {k: np.array([rd(f, k, i) for i in range(ns)]) for k in ("gyraze_converged_count", "gyraze_unconverged_count", "gyraze_fallback_count", "gyraze_potential_avg", "gyraze_cutoff_factor_avg", "gyraze_cutoff_factor_count", "sheath_pot")}
res = {}
for lo, hi in W:
    sel = (g >= lo) & (g <= hi)
    cc, cu, cf = (C[k][sel].sum(0) for k in ("gyraze_converged_count", "gyraze_unconverged_count", "gyraze_fallback_count"))
    tot = cc + cu + cf
    wgt = C["gyraze_cutoff_factor_count"][sel]
    pot = np.where(wgt.sum(0) > 0, (C["gyraze_potential_avg"][sel] * wgt).sum(0) / np.maximum(wgt.sum(0), 1), np.nan)
    fac = np.where(wgt.sum(0) > 0, (C["gyraze_cutoff_factor_avg"][sel] * wgt).sum(0) / np.maximum(wgt.sum(0), 1), np.nan)
    shp = C["sheath_pot"][sel].mean(0)
    res[(lo, hi)] = dict(cc=cc, cu=cu, cf=cf, tot=tot, pot=pot, fac=fac, shp=shp)
fig, axs = plt.subplots(2, 2, figsize=(18, 11))
cols = {"converged": "tab:green", "unconverged": "tab:orange", "fallback": "tab:red"}
for ax, (name, m) in zip(axs[0], (("outer target", outer), ("inner target", inner))):
    r = res[W[0]]; j = np.where(m & (r["tot"] > 0))[0]; j = j[np.argsort(pw[j])]
    x = np.arange(len(j)); t = np.maximum(r["tot"][j], 1)
    ax.bar(x, r["cc"][j] / t, color=cols["converged"], label="converged region")
    ax.bar(x, r["cu"][j] / t, bottom=r["cc"][j] / t, color=cols["unconverged"], label="unconverged (projected NN)")
    ax.bar(x, r["cf"][j] / t, bottom=(r["cc"][j] + r["cu"][j]) / t, color=cols["fallback"], label="fallback (original cutoff)")
    ax2 = ax.twinx(); ax2.plot(x, r["pot"][j], "k.-", label="sheath potential given to GYRAZE [V]"); ax2.set_ylabel("V")
    ax.set_xticks(x); ax.set_xticklabels([f"{p:.3f}" for p in pw[j]], rotation=70, fontsize=8); ax.set_xlabel(r"wall node $\psi_N$ (ordered)")
    ax.set_ylabel("fraction of surrogate calls"); ax.set_title(f"{name}: GYRAZE region per wall node, gstep {W[0][0]}-{W[0][1]}"); ax.legend(loc="upper left", fontsize=8); ax2.legend(loc="upper right", fontsize=8)
ax = axs[1, 0]
for (lo, hi), mk in zip(W, ("o", "s")):
    r = res[(lo, hi)]; j = np.where(div & (r["tot"] > 0))[0]
    dom = np.argmax(np.vstack([r["cc"][j], r["cu"][j], r["cf"][j]]), axis=0)
    for k, (lab, c) in enumerate(cols.items()):
        jj = j[dom == k]
        if len(jj): ax.scatter(r["pot"][jj], r["pot"][jj] / Te[jj], c=c, marker=mk, s=40, label=f"{lab}, g{lo}-{hi}", alpha=.8)
ax.set_xlabel("sheath potential given to GYRAZE [V]"); ax.set_ylabel(r"$e\phi / T_e$ (wall-node $T_e$ from f2d)"); ax.set_title("divertor wall nodes: dominant region vs potential (circle: early window, square: late)"); ax.grid(alpha=.3); ax.legend(fontsize=8)
ax = axs[1, 1]
for (lo, hi), mk in zip(W, ("o", "s")):
    r = res[(lo, hi)]; j = np.where(div & (r["tot"] > 0))[0]
    dom = np.argmax(np.vstack([r["cc"][j], r["cu"][j], r["cf"][j]]), axis=0)
    for k, (lab, c) in enumerate(cols.items()):
        jj = j[dom == k]
        if len(jj): ax.scatter(r["pot"][jj], r["fac"][jj], c=c, marker=mk, s=40, alpha=.8, label=f"{lab}, g{lo}-{hi}")
ax.set_xlabel("sheath potential given to GYRAZE [V]"); ax.set_ylabel("cutoff factor applied"); ax.set_title("divertor wall nodes: cutoff factor vs potential"); ax.grid(alpha=.3); ax.legend(fontsize=8)
fig.suptitle(f"{d.rstrip('/').split('/')[-1]}: GYRAZE surrogate region at the divertor targets, SOL side (psiN >= 1)"); fig.tight_layout(); fig.savefig(out, dpi=140); print("wrote", out)
for (lo, hi) in W:
    r = res[(lo, hi)]
    print(f"\n=== gstep {lo}-{hi}: divertor wall nodes (psiN | R,Z | angle deg | Te eV | phi_GYRAZE V | phi/Te | conv/unconv/fallback % | cutoff factor)")
    for name, m in (("OUTER", outer), ("INNER", inner)):
        j = np.where(m & (r["tot"] > 0))[0]; j = j[np.argsort(pw[j])]; print(f"  {name}: {len(j)} nodes with surrogate calls")
        for k in j:
            t = r["tot"][k]
            print(f"   {pw[k]:.4f} | {Rw[k]:.3f},{Zw[k]:.3f} | {ang[k]:4.1f} | {Te[k]:5.1f} | {r['pot'][k]:7.1f} | {r['pot'][k]/Te[k]:5.1f} | {100*r['cc'][k]/t:5.1f}/{100*r['cu'][k]/t:5.1f}/{100*r['cf'][k]/t:5.1f} | {r['fac'][k]:.3f}")
    j = np.where(div & (r["tot"] > 0))[0]
    print(f"  divertor total calls: converged {r['cc'][j].sum():.0f}, unconverged {r['cu'][j].sum():.0f}, fallback {r['cf'][j].sum():.0f}")
