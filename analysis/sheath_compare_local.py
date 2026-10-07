#!/usr/bin/env python3
"""Sheath comparison on local copies of XGCa run directories (any machine).

usage: python sheath_compare_local.py <out_dir> --steps 60,70,80 [--heat-steps 60,70,80] \
          "<label>=<run_dir>" ["<label>=<run_dir>" ...]
Each run_dir needs xgc.mesh.bp, xgc.equil.bp (first run), xgc.2d.<step>.bp and xgc.f2d.<step>.bp for the
f2d steps (only the LAST f2d step listed is required; earlier ones are used when present), xgc.heatdiag2.bp.
Products: fig1 (wall-node fluxes, f2d), fig2 (heatdiag wall loads), fig3 (potentials), fig4 (time traces),
summary.json. Same definitions as analysis/n641pe_sheath_compare.py; heatdiag 'gstep' or 'step' both handled.
"""
import json, os, sys
import numpy as np, adios2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 13, "axes.titlesize": 13, "axes.labelsize": 13, "legend.fontsize": 11, "lines.linewidth": 2.0, "lines.markersize": 5})

argv = sys.argv[1:]
def popopt(name, default):
    if name in argv:
        k = argv.index(name); v = argv[k + 1]; del argv[k:k + 2]; return tuple(int(x) for x in v.split(","))
    return default
F2D_STEPS = popopt("--steps", (60, 70, 80)); HEAT_STEPS = popopt("--heat-steps", F2D_STEPS)
OUT = os.path.abspath(argv[0]); os.makedirs(OUT, exist_ok=True)
RUNS = [a.split("=", 1) for a in argv[1:]]
COLORS = ["tab:blue", "tab:red", "tab:orange", "tab:cyan", "tab:green", "tab:purple"]
EV = 1.602176634e-19

def rd(path, var, step=None):
    with adios2.FileReader(path) as f:
        return np.squeeze(np.asarray(f.read(var) if step is None else f.read(var, step_selection=[step, 1])))

d0 = RUNS[0][1]
rz = rd(d0 + "/xgc.mesh.bp", "rz"); r, z = rz[:, 0], rz[:, 1]; psi = rd(d0 + "/xgc.mesh.bp", "psi"); wn = rd(d0 + "/xgc.mesh.bp", "grid_wall_nodes") - 1
x_psi = float(rd(d0 + "/xgc.equil.bp", "eq_x_psi")); x_r = float(rd(d0 + "/xgc.equil.bp", "eq_x_r")); x_z = float(rd(d0 + "/xgc.equil.bp", "eq_x_z"))
psin = psi / x_psi; div = z[wn] < x_z
inner_w = np.where(div & (r[wn] < x_r))[0]; inner_w = inner_w[np.argsort(psin[wn][inner_w])]
outer_w = np.where(div & (r[wn] >= x_r))[0]; outer_w = outer_w[np.argsort(psin[wn][outer_w])]
targets_w = {"inner": inner_w, "outer": outer_w}

def heat_geom(d):
    p = d + "/xgc.heatdiag2.bp"
    with adios2.FileReader(p) as f: v = f.available_variables()
    sv = "gstep" if "gstep" in v else "step"; n = int(v[sv]["AvailableStepsCount"])
    st = [int(rd(p, sv, i)) for i in range(n)]; t = [float(rd(p, "time", i)) for i in range(n)]
    return p, st, t, rd(p, "r", 0), rd(p, "z", 0), rd(p, "psi", 0) / x_psi, rd(p, "ds", 0)

def load_run(d):
    o = {"f2d": {}, "heat": {}}
    steps = [s for s in F2D_STEPS if os.path.exists(f"{d}/xgc.f2d.{s:05d}.bp")]
    acc = {}
    for s in steps:
        p = f"{d}/xgc.f2d.{s:05d}.bp"
        for sp in ("e", "i"):
            acc.setdefault(sp + "_n", []).append(rd(p, sp + "_den")[wn])
            acc.setdefault(sp + "_u", []).append((rd(p, sp + "_parallel_flow_df") + rd(p, sp + "_parallel_flow_f0"))[wn])
            acc.setdefault(sp + "_Q", []).append((rd(p, sp + "_par_en_flux_df") + rd(p, sp + "_par_en_flux_f0"))[wn])
            acc.setdefault(sp + "_T", []).append(((rd(p, sp + "_T_para") + 2 * rd(p, sp + "_T_perp")) / 3)[wn])
    o["f2d"] = {k: np.mean(v, 0) for k, v in acc.items()}
    for sp in ("e", "i"): o["f2d"][sp + "_G"] = o["f2d"][sp + "_n"] * o["f2d"][sp + "_u"]
    o["f2d_steps"] = steps
    s2 = [s for s in F2D_STEPS if os.path.exists(f"{d}/xgc.2d.{s:05d}.bp")]
    o["shpot"] = np.mean([rd(f"{d}/xgc.2d.{s:05d}.bp", "shpot") for s in s2], 0)
    o["potwall"] = np.mean([(rd(f"{d}/xgc.2d.{s:05d}.bp", "pot0") + rd(f"{d}/xgc.2d.{s:05d}.bp", "dpot"))[wn] for s in s2], 0)
    import glob, re
    tr = []
    for p2 in sorted(glob.glob(f"{d}/xgc.2d.[0-9]*.bp")):
        s = int(re.search(r"xgc\.2d\.(\d+)\.bp", p2).group(1)); sh = rd(p2, "shpot"); tr.append((s, sh[inner_w].mean(), sh[outer_w].mean()))
    o["trace"] = np.array(tr).reshape(-1, 3)
    p, st, t, hr, hz, hpsin, ds = heat_geom(d)
    dt = t[1] - t[0]
    H = {"step": np.array(st), "r": hr, "z": hz, "psin": hpsin, "ds": ds, "dt": dt}
    for sp in ("e", "i"):
        H[sp + "_N"] = np.array([rd(p, sp + "_number", i)[1:] for i in range(len(st))])
        H[sp + "_E"] = np.array([rd(p, sp + "_para_energy", i)[1:] + rd(p, sp + "_perp_energy", i)[1:] for i in range(len(st))])
    idx = [st.index(s) for s in HEAT_STEPS if s in st]
    for sp in ("e", "i"):
        H[sp + "_Gw"] = H[sp + "_N"][idx].mean(0) / (dt * ds); H[sp + "_qw"] = H[sp + "_E"][idx].mean(0) / (dt * ds)
    o["heat"] = H
    return o

data = {lbl: load_run(d) for lbl, d in RUNS}
H0 = data[RUNS[0][0]]["heat"]; hdiv = H0["z"] < x_z
targets_s = {"inner": np.where(hdiv & (H0["r"] < x_r))[0], "outer": np.where(hdiv & (H0["r"] >= x_r))[0]}
for k in targets_s: targets_s[k] = targets_s[k][np.argsort(H0["psin"][targets_s[k]])]
col = {lbl: COLORS[i] for i, (lbl, _) in enumerate(RUNS)}

summary = {"f2d_steps": F2D_STEPS, "heat_steps": HEAT_STEPS, "runs": {}}
for lbl, D in data.items():
    rec = {"targets": {}, "f2d_steps_used": D["f2d_steps"]}
    for tg, idx in targets_w.items():
        F = D["f2d"]; t = {}
        for sp in ("e", "i"):
            j = idx[np.nanargmax(np.abs(F[sp + "_G"][idx]))]; t[sp + "_Gamma_par_peak"] = float(F[sp + "_G"][j]); t[sp + "_Gamma_par_peak_psin"] = float(psin[wn][j])
            t[sp + "_Q_par_peak_MWm2"] = float(np.nanmax(np.abs(F[sp + "_Q"][idx]))) / 1e6; t[sp + "_T_mean_eV"] = float(np.mean(F[sp + "_T"][idx]))
        t["Gamma_e_over_Gamma_i_mean"] = float(np.nansum(F["e_G"][idx]) / np.nansum(F["i_G"][idx]))
        t["shpot_mean_V"] = float(np.mean(D["shpot"][idx])); t["shpot_over_Te_mean"] = float(np.mean(D["shpot"][idx] / np.maximum(F["e_T"][idx], 1.0)))
        t["potwall_mean_V"] = float(np.mean(D["potwall"][idx]))
        rec["targets"][tg] = t
    H = D["heat"]; rec["heat"] = {}
    idx_t = [list(H["step"]).index(s) for s in HEAT_STEPS if s in list(H["step"])]
    for tg, idx in targets_s.items():
        Pi = H["i_E"][idx_t][:, idx].sum(1).mean() / H["dt"]; Pe = H["e_E"][idx_t][:, idx].sum(1).mean() / H["dt"]
        Ni = H["i_N"][idx_t][:, idx].sum(1).mean() / H["dt"]; Ne = H["e_N"][idx_t][:, idx].sum(1).mean() / H["dt"]
        rec["heat"][tg] = {"P_i_MW": Pi / 1e6, "P_e_MW": Pe / 1e6, "Gamma_e_over_i": Ne / Ni, "I_net_kA": EV * (Ni - Ne) / 1e3,
                           "q_i_peak_MWm2": float(H["i_qw"][idx].max()) / 1e6, "q_e_peak_MWm2": float(H["e_qw"][idx].max()) / 1e6}
    summary["runs"][lbl] = rec
json.dump(summary, open(OUT + "/summary.json", "w"), indent=1)

def tplot(ax, idx, get, ylabel, title, scale=1.0, absval=False):
    for lbl, D in data.items():
        v = get(D)[idx] * scale; v = np.abs(v) if absval else v
        ax.plot(psin[wn][idx], v, "-o", ms=3, lw=1.2, color=col[lbl], label=lbl)
    ax.axvline(1.0, color="k", ls=":", lw=0.8); ax.set_xlabel(r"$\psi_N$"); ax.set_ylabel(ylabel); ax.set_title(title, fontsize=13); ax.grid(alpha=0.3); ax.set_xlim(0.975, 1.10)
fig, axs = plt.subplots(2, 4, figsize=(28, 12))
for i, (tg, idx) in enumerate(targets_w.items()):
    tplot(axs[i, 0], idx, lambda D: D["f2d"]["i_G"], r"$|\Gamma_{\parallel,i}|$", f"{tg}: ion parallel particle flux", absval=True)
    tplot(axs[i, 1], idx, lambda D: D["f2d"]["e_G"], r"$|\Gamma_{\parallel,e}|$", f"{tg}: electron parallel particle flux", absval=True)
    tplot(axs[i, 2], idx, lambda D: D["f2d"]["e_G"] / np.where(np.abs(D["f2d"]["i_G"]) > 0, D["f2d"]["i_G"], np.nan), r"$\Gamma_e/\Gamma_i$", f"{tg}: ambipolarity ratio"); axs[i, 2].axhline(1, color="k", lw=0.8); axs[i, 2].set_ylim(0, 3)
    tplot(axs[i, 3], idx, lambda D: D["f2d"]["i_Q"] + D["f2d"]["e_Q"], r"$|Q_\parallel|$ i+e (MW/m$^2$)", f"{tg}: total parallel heat flux", scale=1e-6, absval=True)
axs[0, 0].legend(fontsize=11); fig.suptitle(f"wall nodes, xgc.f2d averaged over gstep {F2D_STEPS} (along B)"); fig.tight_layout(); fig.savefig(OUT + "/fig1_f2d_target_fluxes.png", dpi=150); plt.close(fig)
fig, axs = plt.subplots(2, 4, figsize=(28, 12))
for i, (tg, idx) in enumerate(targets_s.items()):
    for j, (key, yl, sc) in enumerate((("i_Gw", r"$\Gamma_{i,wall}$", 1), ("e_Gw", r"$\Gamma_{e,wall}$", 1), ("i_qw", r"$q_{i,wall}$ (MW/m$^2$)", 1e-6), ("e_qw", r"$q_{e,wall}$ (MW/m$^2$)", 1e-6))):
        for lbl, D in data.items(): axs[i, j].plot(D["heat"]["psin"][idx], D["heat"][key][idx] * sc, "-", lw=1.2, color=col[lbl], label=lbl)
        axs[i, j].axvline(1, color="k", ls=":", lw=0.8); axs[i, j].set_xlabel(r"$\psi_N$"); axs[i, j].set_ylabel(yl); axs[i, j].set_title(f"{tg}: {yl}", fontsize=13); axs[i, j].grid(alpha=0.3); axs[i, j].set_xlim(0.975, 1.10)
axs[0, 0].legend(fontsize=11); fig.suptitle(f"xgc.heatdiag2 wall segments, averaged over gstep {HEAT_STEPS}"); fig.tight_layout(); fig.savefig(OUT + "/fig2_heatdiag_targets.png", dpi=150); plt.close(fig)
fig, axs = plt.subplots(2, 4, figsize=(28, 12))
for i, (tg, idx) in enumerate(targets_w.items()):
    tplot(axs[i, 0], idx, lambda D: D["shpot"], r"$\phi_{sheath}$ (V)", f"{tg}: sheath potential (shpot)")
    tplot(axs[i, 1], idx, lambda D: D["shpot"] / np.maximum(D["f2d"]["e_T"], 1.0), r"$e\phi_{sheath}/T_e$", f"{tg}: sheath potential / Te")
    tplot(axs[i, 2], idx, lambda D: D["potwall"], r"pot0+dpot at wall (V)", f"{tg}: axisymmetric potential at the wall node")
    tplot(axs[i, 3], idx, lambda D: D["f2d"]["e_T"], r"$T_e$ (eV)", f"{tg}: electron temperature at the wall")
axs[0, 0].legend(fontsize=11); fig.suptitle(f"potentials at the divertor wall nodes, gstep {F2D_STEPS}"); fig.tight_layout(); fig.savefig(OUT + "/fig3_target_potentials.png", dpi=150); plt.close(fig)
fig, axs = plt.subplots(2, 3, figsize=(24, 11))
for i, tg in enumerate(("inner", "outer")):
    idx = targets_s[tg]
    for lbl, D in data.items():
        H = D["heat"]; Pi = H["i_E"][:, idx].sum(1) / H["dt"]; Pe = H["e_E"][:, idx].sum(1) / H["dt"]; Ni = H["i_N"][:, idx].sum(1) / H["dt"]; Ne = H["e_N"][:, idx].sum(1) / H["dt"]
        axs[i, 0].plot(H["step"], Pi / 1e6, "-", color=col[lbl], label=f"{lbl} ions"); axs[i, 0].plot(H["step"], Pe / 1e6, "--", color=col[lbl], label=f"{lbl} electrons")
        axs[i, 1].plot(H["step"], Ne / Ni, "-", color=col[lbl], label=lbl)
        tr = D["trace"]; axs[i, 2].plot(tr[:, 0], tr[:, 1 + i], "-", color=col[lbl], label=lbl)
    axs[i, 0].set_ylabel("P to target (MW)"); axs[i, 0].set_title(f"{tg}: power (heatdiag2)", fontsize=13)
    axs[i, 1].set_ylabel(r"$N_e/N_i$"); axs[i, 1].set_title(f"{tg}: electron/ion particle ratio", fontsize=13); axs[i, 1].axhline(1, color="k", lw=0.8)
    axs[i, 2].set_ylabel(r"mean $\phi_{sheath}$ (V)"); axs[i, 2].set_title(f"{tg}: mean sheath potential", fontsize=13)
    for a in axs[i]: a.set_xlabel("gstep"); a.grid(alpha=0.3)
axs[0, 0].legend(fontsize=10); axs[0, 1].legend(fontsize=10); fig.suptitle("time evolution of target loads and sheath potential"); fig.tight_layout(); fig.savefig(OUT + "/fig4_time_traces.png", dpi=150); plt.close(fig)
print("wrote", OUT); print(json.dumps({l: r["heat"] for l, r in summary["runs"].items()}, indent=1))
