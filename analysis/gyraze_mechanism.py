"""Why the mode-3 wall potential is lower with GYRAZE: outer-target time traces, GYRAZE run vs control.
usage: python gyraze_mechanism.py <gyraze_run> <control_run> <out_png> <gstep_max> <f2d_step_for_Te>"""
import sys, numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 11, "lines.linewidth": 1.8})
dg, dc, out = sys.argv[1:4]; GMAX = int(sys.argv[4]); FS = int(sys.argv[5])
rd = lambda f, k, i=None: np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)
with adios2.FileReader(f"{dg}/xgc.mesh.bp") as f: rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1
with adios2.FileReader(f"{dg}/xgc.equil.bp") as f: xpsi = float(rd(f, "eq_x_psi")[0]); xr = float(rd(f, "eq_x_r")[0]); xz = float(rd(f, "eq_x_z")[0])
pw, Rw, Zw = psi[wall] / xpsi, rz[wall, 0], rz[wall, 1]
outer = (Zw < xz) & (Rw > xr); band = outer & (pw >= 1.0) & (pw <= 1.045); strike = outer & (pw >= 1.0) & (pw <= 1.014)
far = outer & (pw >= 1.03) & (pw <= 1.045)
def sheath(d, keys):
    with adios2.FileReader(f"{d}/xgc.sheathdiag.bp") as f:
        ns = int(f.available_variables()["gstep"]["AvailableStepsCount"]); g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)]); t = np.array([float(rd(f, "time", i)[0]) for i in range(ns)])
        return g, t, {k: np.array([rd(f, k, i) for i in range(ns)]) for k in keys}
gg, tg, G = sheath(dg, ("sheath_pot", "sheath_lost", "sheath_ilost", "gyraze_cutoff_factor_avg", "gyraze_cutoff_factor_count", "gyraze_potential_avg"))
gc, tc, C = sheath(dc, ("sheath_pot", "sheath_lost", "sheath_ilost"))
def cur(t, lost): dt = np.diff(t, prepend=t[0] - (t[1] - t[0])); return lost / dt[:, None]
In_g, Ii_g = cur(tg, G["sheath_lost"]), cur(tg, G["sheath_ilost"]); Ie_g = In_g - Ii_g      # mode 3: lost = net, ilost = ions
In_c, Ii_c = cur(tc, C["sheath_lost"]), cur(tc, C["sheath_ilost"]); Ie_c = In_c - Ii_c
mg, mc = gg <= GMAX, gc <= GMAX
with adios2.FileReader(f"{dg}/xgc.f2d.{FS:05d}.bp") as f: Te_g = ((rd(f, "e_T_para") + 2 * rd(f, "e_T_perp")) / 3)[wall]
with adios2.FileReader(f"{dc}/xgc.f2d.{FS:05d}.bp") as f: Te_c = ((rd(f, "e_T_para") + 2 * rd(f, "e_T_perp")) / 3)[wall]
# call-weighted GYRAZE factor over the strike band, and the mean wall potential there
w = G["gyraze_cutoff_factor_count"][:, strike]; fac = (G["gyraze_cutoff_factor_avg"][:, strike] * w).sum(1) / np.maximum(w.sum(1), 1)
phi_g = G["sheath_pot"][:, strike].mean(1); phi_c = C["sheath_pot"][:, strike].mean(1)
cut_g = (G["gyraze_potential_avg"][:, strike] * G["gyraze_cutoff_factor_avg"][:, strike] * w).sum(1) / np.maximum(w.sum(1), 1)   # applied cutoff energy [eV]
colg, colc = "tab:orange", "tab:blue"
fig, axs = plt.subplots(3, 2, figsize=(18, 14), sharex=True)
ax = axs[0, 0]
ax.plot(gg[mg], phi_g[mg], color=colg, label="GYRAZE run: wall (sheath) potential, strike band mean"); ax.plot(gc[mc], phi_c[mc], color=colc, label="control: wall (sheath) potential, strike band mean")
ax.set_ylabel("V"); ax.set_title(f"(1) outer strike band (psiN 1.0-1.014, {strike.sum()} nodes): wall potential and GYRAZE factor"); ax.grid(alpha=.3)
ax2 = ax.twinx(); ax2.plot(gg[mg], fac[mg], ":", color="gray", label="GYRAZE cutoff factor (right axis, log)"); ax2.set_yscale("log"); ax2.set_ylabel("factor")
h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels(); ax.legend(h1 + h2, l1 + l2, fontsize=8, loc="upper left")
ax = axs[0, 1]
ax.plot(gg[mg], cut_g[mg], color=colg, label="GYRAZE run: applied electron cutoff = phi x factor [eV]")
ax.plot(gc[mc], phi_c[mc], color=colc, label="control: electron cutoff = e phi_wall [eV] (factor 1)")
ax.axhline(3 * Te_g[strike].mean(), color=colg, ls="--", lw=1, label=f"3 Te (GYRAZE run, f2d g{FS}) = {3*Te_g[strike].mean():.0f} eV")
ax.axhline(3 * Te_c[strike].mean(), color=colc, ls="--", lw=1, label=f"3 Te (control, f2d g{FS}) = {3*Te_c[strike].mean():.0f} eV")
ax.set_ylabel("eV"); ax.set_title("(2) electron cutoff energy applied at the strike band"); ax.grid(alpha=.3); ax.legend(fontsize=8)
ax = axs[1, 0]
ax.plot(gg[mg], Ie_g[mg][:, band].sum(1), color=colg, label="GYRAZE run"); ax.plot(gc[mc], Ie_c[mc][:, band].sum(1), color=colc, label="control")
ax.axhline(0, color="k", lw=.8); ax.set_ylabel("A (negative = electrons leaving)"); ax.set_title(f"(3) electron current to the outer target SOL band (psiN 1.0-1.045, {band.sum()} nodes)"); ax.grid(alpha=.3); ax.legend(fontsize=9)
ax = axs[1, 1]
ax.plot(gg[mg], Ii_g[mg][:, band].sum(1), color=colg, label="GYRAZE run"); ax.plot(gc[mc], Ii_c[mc][:, band].sum(1), color=colc, label="control")
ax.axhline(0, color="k", lw=.8); ax.set_ylabel("A (positive = ions arriving)"); ax.set_title("(4) ion current to the same band"); ax.grid(alpha=.3); ax.legend(fontsize=9)
ax = axs[2, 0]
for k, ls in zip(np.where(far)[0], ("-", "--", ":")):
    ax.plot(gg[mg], G["sheath_pot"][mg, k], ls, color=colg, label=f"GYRAZE run, psiN {pw[k]:.3f}"); ax.plot(gc[mc], C["sheath_pot"][mc, k], ls, color=colc, label=f"control, psiN {pw[k]:.3f}")
ax.set_ylabel("V"); ax.set_xlabel("gstep"); ax.set_title("(5) far-SOL hump at the outer target: wall potential at psiN 1.03-1.045"); ax.grid(alpha=.3); ax.legend(fontsize=8)
ax = axs[2, 1]
common = np.intersect1d(gg[mg], gc[mc]); ig = np.searchsorted(gg, common); ic_ = np.searchsorted(gc, common)
ax.plot(common, phi_g[ig] - phi_c[ic_], color="k", label="strike band: phi(GYRAZE) - phi(control)")
ax.plot(common, G["sheath_pot"][ig][:, far].mean(1) - C["sheath_pot"][ic_][:, far].mean(1), color="tab:purple", label="far-SOL band 1.03-1.045: phi(GYRAZE) - phi(control)")
ax.axhline(0, color="k", lw=.8); ax.set_ylabel("V"); ax.set_xlabel("gstep"); ax.set_title("(6) wall-potential difference, GYRAZE minus control"); ax.grid(alpha=.3); ax.legend(fontsize=9)
fig.suptitle(f"Why the conducting-wall (mode 3) potential is lower with GYRAZE: {dg.rstrip('/').split('/')[-1]} vs {dc.rstrip('/').split('/')[-1]}, outer target, gstep <= {GMAX}")
fig.tight_layout(); fig.savefig(out, dpi=130); print("wrote", out)
for lo, hi in ((10, 50), (100, 200), (220, 260), (400, 500)):
    s = (gg >= lo) & (gg <= hi); sc = (gc >= lo) & (gc <= hi)
    print(f"gstep {lo}-{hi}: factor {fac[s].mean():5.2f} | phi strike G/C {phi_g[s].mean():6.1f}/{phi_c[sc].mean():6.1f} V | cutoff G/C {cut_g[s].mean():6.1f}/{phi_c[sc].mean():6.1f} eV | I_e band G/C {Ie_g[s][:, band].sum(1).mean():7.1f}/{Ie_c[sc][:, band].sum(1).mean():7.1f} A | I_i band G/C {Ii_g[s][:, band].sum(1).mean():6.1f}/{Ii_c[sc][:, band].sum(1).mean():6.1f} A | far phi G/C {G['sheath_pot'][s][:, far].mean():6.1f}/{C['sheath_pot'][sc][:, far].mean():6.1f} V")
