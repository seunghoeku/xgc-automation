"""Three-panel map (alpha = 1, 2, 3 deg) of the GYRAZE surrogate converged region with the divertor-SOL wall nodes of a 1x and
a 100x run; each panel shows the nodes whose own grazing angle lies within +-0.75 deg of the panel's alpha. No support vectors."""
import sys, numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 11})
S, out = sys.argv[1:3]; HALF = 0.75
with adios2.FileReader("gyraze_surrogate_mpe.bp") as f: A = {k: np.asarray(f.read(k)) for k in f.available_variables()}
sv = A["svm.support_vectors"].reshape(-1, 3); co = A["svm.coefficients"].reshape(-1); ic = float(A["svm.intercept"][0]); kg = float(A["svm.kernel_gamma"][0])
svm = lambda a, gm, ph: ic + (np.exp(-kg * ((sv - [a, gm, ph]) ** 2).sum(1)) * co).sum()
rd = lambda f, k, i=None: np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)
def pts(run, lo, hi, fs):
    with adios2.FileReader(f"{run}/xgc.mesh.bp") as f: rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1
    with adios2.FileReader(f"{run}/xgc.equil.bp") as f: xpsi = float(rd(f, "eq_x_psi")[0]); xz = float(rd(f, "eq_x_z")[0])
    with adios2.FileReader(f"{run}/xgc.bfield.bp") as f: B = np.linalg.norm(np.asarray(f.read("bfield")), axis=0)[wall]
    with adios2.FileReader(f"{run}/xgc.gyraze.bp") as f: al = np.degrees(rd(f, "bfield_impact_angle"))
    with adios2.FileReader(f"{run}/xgc.f2d.{fs:05d}.bp") as f: Te = ((rd(f, "e_T_para") + 2 * rd(f, "e_T_perp")) / 3)[wall]; ne = rd(f, "e_den")[wall]
    with adios2.FileReader(f"{run}/xgc.sheathdiag.bp") as f:
        ns = int(f.available_variables()["gstep"]["AvailableStepsCount"]); g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)])
        pot = np.array([rd(f, "gyraze_potential_avg", i) for i in range(ns)]); cnt = np.array([rd(f, "gyraze_cutoff_factor_count", i) for i in range(ns)])
    s = (g >= lo) & (g <= hi); w = cnt[s]; p = (pot[s] * w).sum(0) / np.maximum(w.sum(0), 1)
    pw, Zw = psi[wall] / xpsi, rz[wall, 1]; k = np.where((Zw < xz) & (pw >= 1.0) & (w.sum(0) > 0) & (p / Te > 0.01))[0]   # divertor plates, SOL side
    return al[k], np.sqrt(9.10938356e-31 * ne[k] / 8.8541878128e-12) / B[k], p[k] / Te[k]
P1 = pts(f"{S}/d0109sa", 220, 260, 240); P100 = pts(f"{S}/d0111sa", 220, 260, 240)
fig, axs = plt.subplots(1, 3, figsize=(22, 7.5)); gam_ax = np.linspace(0.05, 12, 160); phi_ax = np.linspace(0.05, 20, 160)
for ax, al in zip(axs, (1.0, 2.0, 3.0)):
    Z = np.array([[svm(al, gm, ph) for gm in gam_ax] for ph in phi_ax])
    ax.contourf(gam_ax, phi_ax, Z, levels=[-100, 0, 100], colors=["mistyrose", "honeydew"]); ax.contour(gam_ax, phi_ax, Z, levels=[0], colors="k")
    for (a, gm, ph), mk, c, lab in ((P1, "o", "tab:blue", "d0109sa, 1x density"), (P100, "s", "tab:red", "d0111sa, 100x density")):
        sel = np.abs(a - al) < HALF
        ax.scatter(gm[sel], ph[sel], marker=mk, s=55, c=c, edgecolor="k", label=f"{lab}: {sel.sum()} divertor SOL wall nodes with grazing angle {al-HALF:.2f}-{al+HALF:.2f} deg")
    ax.set_xlabel(r"$\gamma = \omega_{pe}/\omega_{ce}$"); ax.set_ylabel(r"$\phi_N = e\phi/T_e$"); ax.set_xlim(0, 12); ax.set_ylim(0, 20); ax.grid(alpha=.3)
    ax.set_title(f"alpha = {al:.0f} deg"); ax.legend(fontsize=8.5, loc="upper right")
fig.suptitle("GYRAZE surrogate: converged region (green) vs unconverged (red) at three grazing angles, with the TCV divertor SOL wall nodes (gstep 220-260 average)"); fig.tight_layout(); fig.savefig(out, dpi=130); print("wrote", out)
for al in (1.0, 2.0, 3.0):
    print(f"alpha {al:.0f} deg panel: 1x nodes {int((np.abs(P1[0]-al) < HALF).sum())}, 100x nodes {int((np.abs(P100[0]-al) < HALF).sum())}")
