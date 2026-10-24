"""Re-implement the XGC GYRAZE surrogate (surrogate_kokkos.hpp) in numpy: SVM converged region, base/unconv NN -> v_cut(mu), factor.
Map the converged region and the factor at the TCV wall conditions; overlay the d0109sa divertor wall nodes."""
import sys, numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 11})
bp, run, out = sys.argv[1:4]
with adios2.FileReader(bp) as f:
    A = {k: np.asarray(f.read(k)) for k in f.available_variables()}
print("arrays:", ", ".join(f"{k}{list(v.shape)}" for k, v in sorted(A.items())))
def net(prefix):
    g = lambda n: A[f"{prefix}.{n}"].reshape(-1) if A[f"{prefix}.{n}"].ndim == 1 else A[f"{prefix}.{n}"]
    H = g("b0").size
    W = [g(f"W{i}") for i in range(4)]; b = [g(f"b{i}") for i in range(4)]
    W = [w.reshape((H, 3)) if i == 0 else (w.reshape((20, H)) if i == 3 else w.reshape((H, H))) for i, w in enumerate(W)]
    Xm, Xs, Ym, Ys = g("X_mu"), g("X_sigma"), g("Y_mu"), g("Y_sigma")
    def ev(alpha, gamma, phi):
        x = (np.array([alpha, gamma, phi]) - Xm) / Xs
        for i in range(3):
            x = W[i] @ x + b[i]; x = x / (1 + np.exp(-x))
        y = W[3] @ x + b[3]; return np.maximum(y * Ys + Ym, 0.0)
    return ev, H
base, Hb = net("base"); unconv, Hu = net("unconv")
mu_grid = A["base.MU_GRID"].reshape(-1) if "base.MU_GRID" in A else A["MU_GRID"].reshape(-1)
sv = A["svm.support_vectors"].reshape(-1, 3); co = A["svm.coefficients"].reshape(-1); ic = float(A["svm.intercept"].reshape(-1)[0]); kg = float(A["svm.kernel_gamma"].reshape(-1)[0])
def svm(alpha, gamma, phi):
    d2 = ((sv - np.array([alpha, gamma, phi])) ** 2).sum(1); return ic + (np.exp(-kg * d2) * co).sum()
def factor(alpha, gamma, phi, mu_over_ref=1.0):
    conv = svm(alpha, gamma, phi) >= 0
    vcut = (base if conv else unconv)(alpha, gamma, phi)
    v = np.interp(np.clip(mu_over_ref, mu_grid[0], mu_grid[-1]), mu_grid, vcut)
    return max(0.0, v * v / (2 * phi)), conv, v
print(f"hidden widths base {Hb}, unconv {Hu}; mu grid {mu_grid.min():.3g}..{mu_grid.max():.3g} ({mu_grid.size} pts); SVM support vectors {len(co)}")
print("SVM support-vector ranges: alpha %.2f..%.2f deg, gamma %.3f..%.3f, phi_norm %.2f..%.2f" % (sv[:,0].min(), sv[:,0].max(), sv[:,1].min(), sv[:,1].max(), sv[:,2].min(), sv[:,2].max()))
# ---- d0109sa divertor wall nodes: alpha, gamma, phi_norm (two windows)
rd = lambda f, k, i=None: np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)
with adios2.FileReader(f"{run}/xgc.mesh.bp") as f: rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1
with adios2.FileReader(f"{run}/xgc.equil.bp") as f: xpsi = float(rd(f, "eq_x_psi")[0]); xr = float(rd(f, "eq_x_r")[0]); xz = float(rd(f, "eq_x_z")[0])
with adios2.FileReader(f"{run}/xgc.bfield.bp") as f: B = np.linalg.norm(np.asarray(f.read("bfield")), axis=0)[wall]
with adios2.FileReader(f"{run}/xgc.gyraze.bp") as f: alpha_w = np.degrees(rd(f, "bfield_impact_angle"))
pw, Rw, Zw = psi[wall] / xpsi, rz[wall, 0], rz[wall, 1]; div = (Zw < xz) & (pw >= 1.0)
with adios2.FileReader(f"{run}/xgc.sheathdiag.bp") as f:
    ns = int(f.available_variables()["gstep"]["AvailableStepsCount"]); g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)])
    pot = np.array([rd(f, "gyraze_potential_avg", i) for i in range(ns)]); cnt = np.array([rd(f, "gyraze_cutoff_factor_count", i) for i in range(ns)])
    cc = np.array([rd(f, "gyraze_converged_count", i) for i in range(ns)]); cu = np.array([rd(f, "gyraze_unconverged_count", i) for i in range(ns)]); cf = np.array([rd(f, "gyraze_fallback_count", i) for i in range(ns)])
print(f"whole wall, all steps: converged {cc.sum():.3g}, unconverged {cu.sum():.3g}, fallback {cf.sum():.3g} calls -> converged fraction {cc.sum()/(cc.sum()+cu.sum()+cf.sum()):.2e}")
pts = {}
for (lo, hi, fs) in ((220, 260, 240), (900, 1000, 1000)):
    with adios2.FileReader(f"{run}/xgc.f2d.{fs:05d}.bp") as f: Te = ((rd(f, "e_T_para") + 2 * rd(f, "e_T_perp")) / 3)[wall]; ne = rd(f, "e_den")[wall]
    s = (g >= lo) & (g <= hi); w = cnt[s]; ws = np.maximum(w.sum(0), 1); p = (pot[s] * w).sum(0) / ws
    gam = np.sqrt(9.10938356e-31 * ne / 8.8541878128e-12) / B; phin = p / Te
    k = np.where(div & (w.sum(0) > 0) & (phin > 0.01))[0]
    pts[(lo, hi)] = (alpha_w[k], gam[k], phin[k], pw[k])
    print(f"gstep {lo}-{hi}: divertor SOL nodes alpha {alpha_w[k].min():.2f}..{alpha_w[k].max():.2f} deg, gamma {gam[k].min():.2f}..{gam[k].max():.2f}, phi_norm {phin[k].min():.2f}..{phin[k].max():.1f}; "
          f"SVM score min/max {min(svm(a, b, c) for a, b, c in zip(alpha_w[k], gam[k], phin[k])):.2f}/{max(svm(a, b, c) for a, b, c in zip(alpha_w[k], gam[k], phin[k])):.2f}")
# ---- figure
fig, axs = plt.subplots(1, 3, figsize=(22, 7))
gam_ax = np.linspace(0.05, 12.0, 160); phi_ax = np.linspace(0.05, 20, 160)
for ax, al in zip(axs[:2], (1.0, 3.0)):
    Z = np.array([[svm(al, gm, ph) for gm in gam_ax] for ph in phi_ax])
    cs = ax.contourf(gam_ax, phi_ax, Z, levels=[-50, 0, 50], colors=["mistyrose", "honeydew"]); ax.contour(gam_ax, phi_ax, Z, levels=[0], colors="k")
    F = np.array([[factor(al, gm, ph)[0] for gm in gam_ax] for ph in phi_ax])
    c2 = ax.contour(gam_ax, phi_ax, F, levels=[0.5, 0.8, 1.0, 1.5, 2, 3, 5, 10], cmap="viridis"); ax.clabel(c2, fmt="%.1f", fontsize=8)
    for (lo, hi), mk in zip(pts, ("o", "s")):
        a, gm, ph, _ = pts[(lo, hi)]; sel = np.abs(a - al) < (1.0 if al == 1.0 else 1.5)
        ax.scatter(gm[sel], ph[sel], marker=mk, s=40, c="tab:red", edgecolor="k", label=f"d0109sa divertor nodes, g{lo}-{hi}, alpha within band")
    ax.set_xlabel(r"$\gamma = \omega_{pe}/\omega_{ce}$"); ax.set_ylabel(r"$\phi_N = e\phi/T_e$"); ax.set_title(f"alpha = {al} deg: SVM converged region (green, black line = boundary)\nand cutoff factor at mu = T/B (contours)"); ax.legend(fontsize=8, loc="upper right")
ax = axs[2]
for al, c in zip((1.0, 2.0, 3.0, 4.0), ("tab:blue", "tab:orange", "tab:green", "tab:purple")):   # divertor grazing angles are 1.1-4.2 deg
    for gm, ls in zip((0.5, 5.0), ("-", "--")):   # gamma ~0.5 (1x density) and ~5 (100x)
        ph = np.linspace(0.05, 15, 300); fv = np.array([factor(al, gm, p)[0] for p in ph]); cv = np.array([factor(al, gm, p)[1] for p in ph])
        ax.plot(ph, fv, ls, color=c, label=f"alpha {al} deg, gamma {gm}"); ax.plot(ph[cv], fv[cv], "o", color=c, ms=3)
ax.plot([], [], "ko", ms=3, label="dots: SVM says converged")
ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlabel(r"$\phi_N = e\phi/T_e$"); ax.set_ylabel("cutoff factor at mu = T/B"); ax.set_title("factor vs normalized potential at mu = T/B (model curves; solid gamma 0.5 = 1x density, dashed gamma 5 = 100x)"); ax.grid(alpha=.3, which="both"); ax.legend(fontsize=8)
fig.tight_layout(); fig.savefig(out, dpi=130); print("wrote", out)
# ---- cutoff energy in Te: E_cut/Te = factor*phi_N = vcut^2/2
print("\nE_cut/Te = v_cut^2/2 at mu=T/B (alpha 2 deg, gamma 0.5):")
for ph in (0.1, 0.3, 1, 2, 3, 5, 10, 20):
    fct, conv, v = factor(2.0, 0.5, ph); print(f"   phi_N {ph:5.1f}: factor {fct:6.2f}, E_cut/Te {fct*ph:5.2f}, v_cut {v:.2f}, {'converged' if conv else 'unconverged'}")
print("mu dependence at phi_N 2, alpha 2, gamma 0.5:", " ".join(f"mu/muref {m:.2g}: {factor(2.0, 0.5, 2.0, m)[0]:.2f}" for m in (0.01, 0.1, 0.5, 1, 2, 5)))
