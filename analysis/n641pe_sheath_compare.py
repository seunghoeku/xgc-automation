#!/usr/bin/env python3
"""n641pe TCV XGCa sheath-model comparison: dielectric (mode 1) vs conducting
(mode 3), each with and without the GYRAZE surrogate.

Runs on a Perlmutter login node (file-access bound, a few minutes) with the
myenv3_12 conda python. Writes small JSON + PNG products to OUT_DIR.

Quantities
- xgc.f2d.<step>.bp at wall nodes (steps F2D_STEPS, averaged):
    Gamma_par = n * u_par          [m^-2 s^-1]  (positive along B)
    Q_par     = par_en_flux_df+f0  [W m^-2]     (density-weighted, along B)
    T         = (T_para + 2 T_perp)/3           [eV]
- xgc.2d.<step>.bp: shpot (sheath potential per wall node, V), pot0+dpot at
  wall nodes (axisymmetric potential on the wall, V).
- xgc.heatdiag2.bp (accumulated over diag_1d_period = 10 steps): per wall
  segment Gamma_wall = number/(dt_acc ds), q_wall = (E_par+E_perp)/(dt_acc ds),
  P_target = sum(E)/dt_acc.  ds is the segment area (m^2, full torus).
- xgc.sheathdiag.bp: gyraze_potential_avg where present.
Targets: wall nodes/segments below the X-point; inner = R < R_x, outer = R >= R_x.
"""
import json, os, sys
import numpy as np
import adios2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = "/pscratch/sd/s/sku/n641pe_TCV_xgca_gyraze"
RUNS = {  # label -> (dir, sheath mode, gyraze)
    "dielectric (m1)": ("sheath_m1_3", 1, False),
    "dielectric+GYRAZE": ("gyraze_m1_3", 1, True),
    "conducting (m3)": ("sheath_m3_2", 3, False),
    "conducting+GYRAZE": ("gyraze_m3_2", 3, True),
}
COLORS = {"dielectric (m1)": "tab:blue", "dielectric+GYRAZE": "tab:cyan",
          "conducting (m3)": "tab:red", "conducting+GYRAZE": "tab:orange"}
F2D_STEPS = tuple(int(x) for x in os.environ.get("F2D_STEPS", "130,140,150").split(","))  # window for 2d/f2d; override with F2D_STEPS=60,70,80
HEAT_STEPS = (60, 70, 80)          # common heatdiag window (gyraze_m3_2 heatdiag stops at 80)
HEAT_STEPS_LATE = (130, 140, 150)  # three runs only
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "summaries", "sheath_compare")
EXCLUDE = [x for x in os.environ.get("EXCLUDE", "").split(";") if x]
for x in EXCLUDE: RUNS.pop(x, None)
os.makedirs(OUT, exist_ok=True)
EV = 1.602176634e-19

def rd(path, var, step=None):
    with adios2.FileReader(path) as f:
        if step is None:
            return np.asarray(f.read(var))
        return np.asarray(f.read(var, step_selection=[step, 1]))

def steps_in(path, var):
    with adios2.FileReader(path) as f:
        return int(f.available_variables()[var]["AvailableStepsCount"])

# ---------------- mesh / geometry (same for all runs) ----------------
p0 = os.path.join(BASE, "sheath_m3_2")
rz = rd(p0 + "/xgc.mesh.bp", "rz"); r, z = rz[:, 0], rz[:, 1]
psi = rd(p0 + "/xgc.mesh.bp", "psi")
wn = rd(p0 + "/xgc.mesh.bp", "grid_wall_nodes") - 1
region = rd(p0 + "/xgc.mesh.bp", "region")
x_psi = float(rd(p0 + "/xgc.equil.bp", "eq_x_psi")); x_r = float(rd(p0 + "/xgc.equil.bp", "eq_x_r")); x_z = float(rd(p0 + "/xgc.equil.bp", "eq_x_z"))
ax_r = float(rd(p0 + "/xgc.equil.bp", "eq_axis_r")); ax_z = float(rd(p0 + "/xgc.equil.bp", "eq_axis_z"))
psin = psi / x_psi
impact = rd(os.path.join(BASE, "gyraze_m3_2", "xgc.gyraze.bp"), "bfield_impact_angle")  # per wall node, rad
theta_w = np.mod(np.arctan2(z[wn] - ax_z, r[wn] - ax_r), 2 * np.pi)
div = z[wn] < x_z
inner_w = np.where(div & (r[wn] < x_r))[0]; inner_w = inner_w[np.argsort(psin[wn][inner_w])]
outer_w = np.where(div & (r[wn] >= x_r))[0]; outer_w = outer_w[np.argsort(psin[wn][outer_w])]
targets_w = {"inner": inner_w, "outer": outer_w}

# heatdiag segment geometry
hp = p0 + "/xgc.heatdiag2.bp"
seg_r = rd(hp, "r", 0); seg_z = rd(hp, "z", 0); seg_psi = rd(hp, "psi", 0); seg_ds = rd(hp, "ds", 0); seg_ang = rd(hp, "strike_angle", 0)
seg_psin = seg_psi / x_psi  # heatdiag2 psi is unnormalized
seg_div = seg_z < x_z
inner_s = np.where(seg_div & (seg_r < x_r))[0]; inner_s = inner_s[np.argsort(seg_psin[inner_s])]
outer_s = np.where(seg_div & (seg_r >= x_r))[0]; outer_s = outer_s[np.argsort(seg_psin[outer_s])]
targets_s = {"inner": inner_s, "outer": outer_s}
ht = [float(np.ravel(rd(hp, "time", i))[0]) for i in range(2)]
DT_ACC = ht[1] - ht[0]  # accumulation window of one heatdiag output (10 steps)

# ---------------- per-run loading ----------------
def load_f2d(d):
    acc = {}
    for s in F2D_STEPS:
        p = f"{d}/xgc.f2d.{s:05d}.bp"
        with adios2.FileReader(p) as f:
            for sp in ("e", "i"):
                n = f.read(sp + "_den")[wn]
                u = (f.read(sp + "_parallel_flow_df") + f.read(sp + "_parallel_flow_f0"))[wn]
                Q = (f.read(sp + "_par_en_flux_df") + f.read(sp + "_par_en_flux_f0"))[wn]
                T = ((f.read(sp + "_T_para") + 2.0 * f.read(sp + "_T_perp")) / 3.0)[wn]
                for k, v in (("n", n), ("u", u), ("Q", Q), ("T", T)):
                    acc.setdefault(sp + "_" + k, []).append(v)
    out = {k: np.mean(v, axis=0) for k, v in acc.items()}
    for sp in ("e", "i"):
        out[sp + "_G"] = out[sp + "_n"] * out[sp + "_u"]
    return out

def load_2d(d, steps):
    sh, pw = [], []
    for s in steps:
        p = f"{d}/xgc.2d.{s:05d}.bp"
        with adios2.FileReader(p) as f:
            shp = np.squeeze(f.read("shpot")); pot = np.squeeze(f.read("pot0")) + np.squeeze(f.read("dpot"))
        sh.append(shp); pw.append(pot[wn])
    return np.mean(sh, axis=0), np.mean(pw, axis=0)

def shpot_trace(d):
    out = []
    s = 10
    while os.path.exists(f"{d}/xgc.2d.{s:05d}.bp"):
        with adios2.FileReader(f"{d}/xgc.2d.{s:05d}.bp") as f:
            shp = np.squeeze(f.read("shpot"))
        out.append((s, float(np.mean(shp[inner_w])), float(np.mean(shp[outer_w]))))
        s += 10
    return np.array(out)

def load_heat(d):
    p = d + "/xgc.heatdiag2.bp"
    n = steps_in(p, "step")
    rec = {"step": [], "time": []}
    for sp in ("e", "i"):
        for k in ("number", "para_energy", "perp_energy", "potential"):
            rec[sp + "_" + k] = []
    for i in range(n):
        rec["step"].append(int(np.ravel(rd(p, "step", i))[0])); rec["time"].append(float(np.ravel(rd(p, "time", i))[0]))
        for sp in ("e", "i"):
            for k in ("number", "para_energy", "perp_energy", "potential"):
                rec[sp + "_" + k].append(np.squeeze(rd(p, f"{sp}_{k}", i))[1:])  # drop bin 0 (unassigned)
    return {k: np.array(v) for k, v in rec.items()}

def heat_window(H, steps):
    idx = [list(H["step"]).index(s) for s in steps if s in list(H["step"])]
    if not idx:
        return None
    out = {}
    for sp in ("e", "i"):
        N = np.mean(H[sp + "_number"][idx], axis=0); E = np.mean(H[sp + "_para_energy"][idx] + H[sp + "_perp_energy"][idx], axis=0)
        P = np.mean(H[sp + "_potential"][idx], axis=0)
        out[sp + "_Gw"] = N / (DT_ACC * seg_ds)          # m^-2 s^-1 (wall normal)
        out[sp + "_qw"] = E / (DT_ACC * seg_ds)          # W m^-2 (wall normal)
        out[sp + "_Pseg"] = E / DT_ACC                   # W per segment
        with np.errstate(invalid="ignore", divide="ignore"):
            out[sp + "_pot_hit"] = np.where(N > 0, P / N, np.nan)  # hit-weighted sheath potential (V), if accumulated the same way
    return out

def gyraze_pot(d):
    p = d + "/xgc.sheathdiag.bp"
    with adios2.FileReader(p) as f:
        v = f.available_variables()
        if "gyraze_potential_avg" not in v:
            return None, None
        n = int(v["gyraze_potential_avg"]["AvailableStepsCount"])
        g = np.array([f.read("gyraze_potential_avg", step_selection=[i, 1]) for i in range(n)])
        sp = np.array([np.squeeze(f.read("sheath_pot", step_selection=[i, 1])) for i in range(n)])
    return g, sp

data = {}
for lbl, (sub, mode, gz) in RUNS.items():
    d = os.path.join(BASE, sub)
    print("loading", lbl, flush=True)
    F = load_f2d(d)
    shp, pw = load_2d(d, F2D_STEPS)
    H = load_heat(d)
    data[lbl] = dict(f2d=F, shpot=shp, potwall=pw, heat=H, hw=heat_window(H, HEAT_STEPS), hw_late=heat_window(H, HEAT_STEPS_LATE),
                     trace=shpot_trace(d), gyraze=gyraze_pot(d), mode=mode, gz=gz, dir=sub)

# ---------------- summary JSON ----------------
def peak(v, idx):
    j = idx[np.nanargmax(np.abs(v[idx]))]
    return float(v[j]), float(psin[wn][j])

summary = {"base": BASE, "f2d_steps": F2D_STEPS, "heat_steps": HEAT_STEPS, "heat_steps_late": HEAT_STEPS_LATE, "dt_acc_s": DT_ACC,
           "x_point": {"r": x_r, "z": x_z, "psi": x_psi}, "runs": {}}
for lbl, D in data.items():
    F = D["f2d"]; rec = {"dir": D["dir"], "sheath_mode": D["mode"], "gyraze": D["gz"], "targets": {}}
    for tg, idx in targets_w.items():
        t = {}
        for sp in ("e", "i"):
            t[f"{sp}_Gamma_par_peak"], t[f"{sp}_Gamma_par_peak_psin"] = peak(F[sp + "_G"], idx)
            t[f"{sp}_Q_par_peak_MWm2"] = peak(F[sp + "_Q"], idx)[0] / 1e6
            t[f"{sp}_T_mean_eV"] = float(np.mean(F[sp + "_T"][idx]))
        t["Gamma_e_over_Gamma_i_mean"] = float(np.nansum(F["e_G"][idx]) / np.nansum(F["i_G"][idx]))
        t["shpot_mean_V"] = float(np.mean(D["shpot"][idx])); t["shpot_over_Te_mean"] = float(np.mean(D["shpot"][idx] / np.maximum(F["e_T"][idx], 1.0)))
        t["potwall_mean_V"] = float(np.mean(D["potwall"][idx]))
        t["shpot_at_strike_V"] = float(D["shpot"][idx][np.argmin(np.abs(psin[wn][idx] - 1.0))])
        rec["targets"][tg] = t
    for key, hw in (("heat_window", D["hw"]), ("heat_window_late", D["hw_late"])):
        if hw is None:
            rec[key] = None; continue
        rec[key] = {}
        for tg, idx in targets_s.items():
            Pi = float(np.sum(hw["i_Pseg"][idx])); Pe = float(np.sum(hw["e_Pseg"][idx]))
            Gi = float(np.sum(hw["i_Gw"][idx] * seg_ds[idx])); Ge = float(np.sum(hw["e_Gw"][idx] * seg_ds[idx]))
            rec[key][tg] = {"P_i_MW": Pi / 1e6, "P_e_MW": Pe / 1e6, "P_tot_MW": (Pi + Pe) / 1e6,
                            "N_i_per_s": Gi, "N_e_per_s": Ge, "Gamma_e_over_Gamma_i": Ge / Gi if Gi else None,
                            "I_net_A": EV * (Gi - Ge),
                            "q_i_peak_MWm2": float(np.nanmax(hw["i_qw"][idx])) / 1e6, "q_e_peak_MWm2": float(np.nanmax(hw["e_qw"][idx])) / 1e6}
    summary["runs"][lbl] = rec
with open(os.path.join(OUT, "sheath_compare_summary.json"), "w") as f:
    json.dump(summary, f, indent=1)

# save compact arrays for local re-plotting
np.savez_compressed(os.path.join(OUT, "sheath_compare_arrays.npz"),
                    psin_wall=psin[wn], r_wall=r[wn], z_wall=z[wn], theta_wall=theta_w, impact=impact, inner_w=inner_w, outer_w=outer_w,
                    seg_psin=seg_psin, seg_r=seg_r, seg_z=seg_z, seg_ds=seg_ds, seg_angle=seg_ang, inner_s=inner_s, outer_s=outer_s,
                    **{f"{lbl}|{k}": v for lbl, D in data.items() for k, v in D["f2d"].items()},
                    **{f"{lbl}|shpot": D["shpot"] for lbl, D in data.items()},
                    **{f"{lbl}|potwall": D["potwall"] for lbl, D in data.items()},
                    **{f"{lbl}|trace": D["trace"] for lbl, D in data.items()},
                    **{f"{lbl}|heat_{k}": v for lbl, D in data.items() for k, v in D["heat"].items()})

# ---------------- figures ----------------
def tplot(ax, tg_idx, get, ylabel, title, scale=1.0, absval=False):
    for lbl, D in data.items():
        v = get(D)[tg_idx] * scale
        if absval: v = np.abs(v)
        ax.plot(psin[wn][tg_idx], v, "-o", ms=3, lw=1.2, color=COLORS[lbl], label=lbl)
    ax.axvline(1.0, color="k", ls=":", lw=0.8); ax.set_xlabel(r"$\psi_N$"); ax.set_ylabel(ylabel); ax.set_title(title, fontsize=10); ax.grid(alpha=0.3)

# 1. f2d fluxes at the targets
fig, axs = plt.subplots(2, 4, figsize=(20, 8.5))
for i, (tg, idx) in enumerate(targets_w.items()):
    tplot(axs[i, 0], idx, lambda D: D["f2d"]["i_G"], r"$|\Gamma_{\parallel,i}|$ (m$^{-2}$s$^{-1}$)", f"{tg} target: ion parallel particle flux", absval=True)
    tplot(axs[i, 1], idx, lambda D: D["f2d"]["e_G"], r"$|\Gamma_{\parallel,e}|$ (m$^{-2}$s$^{-1}$)", f"{tg} target: electron parallel particle flux", absval=True)
    tplot(axs[i, 2], idx, lambda D: D["f2d"]["e_G"] / np.where(np.abs(D["f2d"]["i_G"]) > 0, D["f2d"]["i_G"], np.nan), r"$\Gamma_{e}/\Gamma_{i}$", f"{tg} target: ambipolarity ratio")
    axs[i, 2].axhline(1.0, color="k", lw=0.8); axs[i, 2].set_ylim(0, 3)
    tplot(axs[i, 3], idx, lambda D: D["f2d"]["i_Q"] + D["f2d"]["e_Q"], r"$|Q_\parallel|$ i+e (MW/m$^2$)", f"{tg} target: total parallel heat flux", scale=1e-6, absval=True)
axs[0, 0].legend(fontsize=8)
fig.suptitle(f"n641pe TCV XGCa, wall nodes, xgc.f2d averaged over gstep {F2D_STEPS} (along B)")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig1_f2d_target_fluxes.png"), dpi=130); plt.close(fig)

# 2. heatdiag wall-normal fluxes
def splot(ax, idx, key, ylabel, title, scale=1.0, which="hw"):
    for lbl, D in data.items():
        hw = D[which]
        if hw is None: continue
        ax.plot(seg_psin[idx], hw[key][idx] * scale, "-", lw=1.2, color=COLORS[lbl], label=lbl)
    ax.axvline(1.0, color="k", ls=":", lw=0.8); ax.set_xlabel(r"$\psi_N$"); ax.set_ylabel(ylabel); ax.set_title(title, fontsize=10); ax.grid(alpha=0.3)
for which, steps, tag in (("hw", HEAT_STEPS, "fig2_heatdiag_targets_gstep60-80.png"), ("hw_late", HEAT_STEPS_LATE, "fig2b_heatdiag_targets_gstep130-150.png")):
    fig, axs = plt.subplots(2, 4, figsize=(20, 8.5))
    for i, (tg, idx) in enumerate(targets_s.items()):
        splot(axs[i, 0], idx, "i_Gw", r"$\Gamma_{i,wall}$ (m$^{-2}$s$^{-1}$)", f"{tg}: ion flux to wall", which=which)
        splot(axs[i, 1], idx, "e_Gw", r"$\Gamma_{e,wall}$ (m$^{-2}$s$^{-1}$)", f"{tg}: electron flux to wall", which=which)
        splot(axs[i, 2], idx, "i_qw", r"$q_{i,wall}$ (MW/m$^2$)", f"{tg}: ion heat load (wall normal)", scale=1e-6, which=which)
        splot(axs[i, 3], idx, "e_qw", r"$q_{e,wall}$ (MW/m$^2$)", f"{tg}: electron heat load (wall normal)", scale=1e-6, which=which)
    axs[0, 0].legend(fontsize=8)
    fig.suptitle(f"n641pe: xgc.heatdiag2 wall segments, averaged over gstep {steps} (accumulated per {DT_ACC*1e6:.2f} us)")
    fig.tight_layout(); fig.savefig(os.path.join(OUT, tag), dpi=130); plt.close(fig)

# 3. potentials at the targets
fig, axs = plt.subplots(2, 4, figsize=(20, 8.5))
for i, (tg, idx) in enumerate(targets_w.items()):
    tplot(axs[i, 0], idx, lambda D: D["shpot"], r"$\phi_{sheath}$ (V)", f"{tg}: sheath potential (xgc.2d shpot)")
    tplot(axs[i, 1], idx, lambda D: D["shpot"] / np.maximum(D["f2d"]["e_T"], 1.0), r"$e\phi_{sheath}/T_e$", f"{tg}: sheath potential / Te")
    tplot(axs[i, 2], idx, lambda D: D["potwall"], r"$\phi_{wall}$ = pot0+dpot (V)", f"{tg}: axisymmetric potential at the wall node")
    tplot(axs[i, 3], idx, lambda D: D["f2d"]["e_T"], r"$T_e$ (eV)", f"{tg}: electron temperature at the wall")
    for lbl, D in data.items():
        g, sp = D["gyraze"]
        if g is not None:
            axs[i, 0].plot(psin[wn][idx], g[-1][idx], "--", color=COLORS[lbl], lw=1, label=f"{lbl}: GYRAZE pot (last sheathdiag)")
axs[0, 0].legend(fontsize=7)
fig.suptitle(f"n641pe: potentials at the divertor wall nodes, averaged over gstep {F2D_STEPS}")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig3_target_potentials.png"), dpi=130); plt.close(fig)

# 4. time traces: target-integrated power/particles (heatdiag) and mean shpot (2d)
fig, axs = plt.subplots(2, 3, figsize=(17, 8))
for i, tg in enumerate(("inner", "outer")):
    idx = targets_s[tg]
    for lbl, D in data.items():
        H = D["heat"]
        Pi = np.sum(H["i_para_energy"][:, idx] + H["i_perp_energy"][:, idx], axis=1) / DT_ACC
        Pe = np.sum(H["e_para_energy"][:, idx] + H["e_perp_energy"][:, idx], axis=1) / DT_ACC
        Ni = np.sum(H["i_number"][:, idx], axis=1) / DT_ACC; Ne = np.sum(H["e_number"][:, idx], axis=1) / DT_ACC
        axs[i, 0].plot(H["step"], Pi / 1e6, "-", color=COLORS[lbl], label=f"{lbl} ions"); axs[i, 0].plot(H["step"], Pe / 1e6, "--", color=COLORS[lbl], label=f"{lbl} electrons")
        axs[i, 1].plot(H["step"], Ne / Ni, "-", color=COLORS[lbl], label=lbl)
        tr = D["trace"]; axs[i, 2].plot(tr[:, 0], tr[:, 1 + i], "-", color=COLORS[lbl], label=lbl)
    axs[i, 0].set_ylabel("P to target (MW)"); axs[i, 0].set_title(f"{tg} target: power (heatdiag2)", fontsize=10)
    axs[i, 1].set_ylabel(r"$N_e/N_i$ to target"); axs[i, 1].set_title(f"{tg} target: electron/ion particle ratio", fontsize=10); axs[i, 1].axhline(1, color="k", lw=0.8)
    axs[i, 2].set_ylabel(r"mean $\phi_{sheath}$ (V)"); axs[i, 2].set_title(f"{tg} target: mean sheath potential (xgc.2d)", fontsize=10)
    for a in axs[i]: a.set_xlabel("gstep"); a.grid(alpha=0.3)
axs[0, 0].legend(fontsize=7); axs[0, 1].legend(fontsize=7)
fig.suptitle("n641pe: time evolution of target loads and sheath potential")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig4_time_traces.png"), dpi=130); plt.close(fig)

# 5. full wall vs poloidal angle
order = np.argsort(theta_w)
fig, axs = plt.subplots(4, 1, figsize=(14, 13), sharex=True)
for lbl, D in data.items():
    axs[0].plot(theta_w[order], D["shpot"][order], ".-", ms=3, lw=0.8, color=COLORS[lbl], label=lbl)
    axs[1].plot(theta_w[order], (D["shpot"] / np.maximum(D["f2d"]["e_T"], 1.0))[order], ".-", ms=3, lw=0.8, color=COLORS[lbl])
    axs[2].plot(theta_w[order], np.abs(D["f2d"]["i_G"])[order], ".-", ms=3, lw=0.8, color=COLORS[lbl])
    axs[3].plot(theta_w[order], np.abs(D["f2d"]["i_Q"] + D["f2d"]["e_Q"])[order] / 1e6, ".-", ms=3, lw=0.8, color=COLORS[lbl])
axs[0].set_ylabel(r"$\phi_{sheath}$ (V)"); axs[1].set_ylabel(r"$e\phi_{sheath}/T_e$"); axs[2].set_ylabel(r"$|\Gamma_{\parallel,i}|$ (m$^{-2}$s$^{-1}$)"); axs[3].set_ylabel(r"$|Q_\parallel|$ i+e (MW/m$^2$)")
axs[3].set_xlabel("poloidal angle from the magnetic axis (rad); divertor near 3pi/2")
for a in axs: a.grid(alpha=0.3)
for a in axs: a.axvspan(theta_w[div].min(), theta_w[div].max(), color="0.9", zorder=0)
axs[0].legend(fontsize=8); fig.suptitle(f"n641pe: all wall nodes, gstep {F2D_STEPS} average (grey = below X-point)")
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig5_full_wall.png"), dpi=130); plt.close(fig)
print("done ->", OUT)
print(json.dumps({l: {"targets": {t: {k: v for k, v in rec["targets"][t].items()}} for t in rec["targets"]} for l, rec in summary["runs"].items()}, indent=1)[:6000])
