"""Ion flux into the outer target: parallel channel (n u_par sin(alpha)) vs ExB drift channel (n v_E.n_out), GYRAZE run vs control.
usage: python ion_flux_channels.py <gyraze_run> <control_run> <out_png> <step>"""
import sys, numpy as np, adios2
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
plt.rcParams.update({"font.size": 11})
dg, dc, out = sys.argv[1:4]; STEP = int(sys.argv[4])
rd = lambda f, k, i=None: np.asarray(f.read(k) if i is None else f.read(k, step_selection=[i, 1])).reshape(-1)
with adios2.FileReader(f"{dg}/xgc.mesh.bp") as f: rz = f.read("rz"); psi = f.read("psi"); wall = f.read("grid_wall_nodes") - 1; tri = np.asarray(f.read("nd_connect_list"))
with adios2.FileReader(f"{dg}/xgc.equil.bp") as f: xpsi = float(rd(f, "eq_x_psi")[0]); xr = float(rd(f, "eq_x_r")[0]); xz = float(rd(f, "eq_x_z")[0])
with adios2.FileReader(f"{dg}/xgc.bfield.bp") as f: B = np.asarray(f.read("bfield"))
with adios2.FileReader(f"{dg}/xgc.gyraze.bp") as f: alpha = rd(f, "bfield_impact_angle")
R, Z = rz[:, 0], rz[:, 1]; n = len(R); tri = tri - tri.min()
pw, Rw, Zw = psi[wall] / xpsi, R[wall], Z[wall]; band = (Zw < xz) & (Rw > xr) & (pw >= 1.0) & (pw <= 1.045); j = np.where(band)[0]; j = j[np.argsort(pw[j])]
# outward wall normal: tangent from the wall-node ordering, sign chosen so that the normal points away from the nearest interior node
isw = np.zeros(n, bool); isw[wall] = True; interior = np.where(~isw)[0]
tR = np.roll(Rw, -1) - np.roll(Rw, 1); tZ = np.roll(Zw, -1) - np.roll(Zw, 1); L = np.hypot(tR, tZ); tR, tZ = tR / L, tZ / L; nR, nZ = tZ, -tR
for i in range(len(wall)):
    d2 = (R[interior] - Rw[i]) ** 2 + (Z[interior] - Zw[i]) ** 2; k = interior[np.argmin(d2)]
    if (R[k] - Rw[i]) * nR[i] + (Z[k] - Zw[i]) * nZ[i] > 0: nR[i], nZ[i] = -nR[i], -nZ[i]
def grad_nodes(phi):
    gR = np.zeros(n); gZ = np.zeros(n); cnt = np.zeros(n); a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    det = (R[b] - R[a]) * (Z[c] - Z[a]) - (R[c] - R[a]) * (Z[b] - Z[a])
    dR = ((phi[b] - phi[a]) * (Z[c] - Z[a]) - (phi[c] - phi[a]) * (Z[b] - Z[a])) / det
    dZ = ((phi[c] - phi[a]) * (R[b] - R[a]) - (phi[b] - phi[a]) * (R[c] - R[a])) / det
    for v in (a, b, c): np.add.at(gR, v, dR); np.add.at(gZ, v, dZ); np.add.at(cnt, v, 1)
    return gR / np.maximum(cnt, 1), gZ / np.maximum(cnt, 1)
def channels(d):
    with adios2.FileReader(f"{d}/xgc.2d.{STEP:05d}.bp") as f: phi = rd(f, "pot0") + rd(f, "dpot")
    with adios2.FileReader(f"{d}/xgc.f2d.{STEP:05d}.bp") as f: ni = rd(f, "i_den"); ui = rd(f, "i_parallel_flow_df") + rd(f, "i_parallel_flow_f0")
    gR, gZ = grad_nodes(phi); ER, EZ = -gR, -gZ; B2 = (B ** 2).sum(0); Bphi = B[2]
    vER, vEZ = EZ * Bphi / B2, -ER * Bphi / B2                      # (E x B)/B^2 poloidal components
    vEn = vER[wall] * nR + vEZ[wall] * nZ                            # positive = drift into the wall
    with adios2.FileReader(f"{d}/xgc.sheathdiag.bp") as f:
        ns = int(f.available_variables()["gstep"]["AvailableStepsCount"]); g = np.array([int(rd(f, "gstep", i)[0]) for i in range(ns)]); t = np.array([float(rd(f, "time", i)[0]) for i in range(ns)]); il = np.array([rd(f, "sheath_ilost", i) for i in range(ns)])
    Ii = (il / np.diff(t, prepend=t[0] - (t[1] - t[0]))[:, None])[(g >= STEP - 20) & (g <= STEP + 20)].mean(0)
    return dict(phi=phi[wall], E=np.hypot(ER, EZ)[wall], vEn=vEn, upar_n=np.abs(ui[wall]) * np.sin(alpha), ni=ni[wall], Ii=Ii)
G, C = channels(dg), channels(dc)
print(f"gstep {STEP}: psiN | n_i G/C [1e18] | v_ExB.n_out G/C [km/s] | u_par sin(a) G/C [km/s] | total normal speed G/C | n*(v_E.n + u sin a) G/C [1e21 /m2s] | I_i G/C [A] | ratio flux | ratio I_i")
for k in j:
    tg, tc = G["vEn"][k] + G["upar_n"][k], C["vEn"][k] + C["upar_n"][k]
    print(f"  {pw[k]:.4f} | {G['ni'][k]/1e18:5.2f}/{C['ni'][k]/1e18:5.2f} | {G['vEn'][k]/1e3:6.2f}/{C['vEn'][k]/1e3:6.2f} | {G['upar_n'][k]/1e3:5.2f}/{C['upar_n'][k]/1e3:5.2f} | {tg/1e3:6.2f}/{tc/1e3:6.2f} | {G['ni'][k]*tg/1e21:6.2f}/{C['ni'][k]*tc/1e21:6.2f} | {G['Ii'][k]:5.1f}/{C['Ii'][k]:5.1f} | {G['ni'][k]*tg/max(C['ni'][k]*tc,1e-9):5.2f} | {G['Ii'][k]/max(C['Ii'][k],1e-9):4.2f}")
Fg = (G["ni"] * (G["vEn"] + G["upar_n"]))[j]; Fc = (C["ni"] * (C["vEn"] + C["upar_n"]))[j]
print(f"band: sum n*(v_E.n+u sin a) G/C {Fg.sum()/1e21:.1f}/{Fc.sum()/1e21:.1f} (ratio {Fg.sum()/Fc.sum():.2f}); parallel part only {(G['ni']*G['upar_n'])[j].sum()/1e21:.1f}/{(C['ni']*C['upar_n'])[j].sum()/1e21:.1f}; ExB part only {(G['ni']*G['vEn'])[j].sum()/1e21:.1f}/{(C['ni']*C['vEn'])[j].sum()/1e21:.1f}; measured I_i {G['Ii'][j].sum():.1f}/{C['Ii'][j].sum():.1f} A (ratio {G['Ii'][j].sum()/C['Ii'][j].sum():.2f})")
fig, axs = plt.subplots(1, 3, figsize=(20, 6)); x = pw[j]
axs[0].plot(x, G["phi"][j], "o-", color="tab:orange", label="GYRAZE run"); axs[0].plot(x, C["phi"][j], "o-", color="tab:blue", label="control"); axs[0].set_title(f"wall potential along the outer target (xgc.2d, g{STEP})"); axs[0].set_ylabel("V")
axs[1].plot(x, G["E"][j] / 1e3, "o-", color="tab:orange", label="GYRAZE: |grad phi| at the wall node"); axs[1].plot(x, C["E"][j] / 1e3, "o-", color="tab:blue", label="control: |grad phi| at the wall node")
ax1b = axs[1].twinx(); ax1b.plot(x, G["ni"][j] / 1e18, "s--", color="tab:orange", alpha=.7, label="GYRAZE: n_i at the wall node"); ax1b.plot(x, C["ni"][j] / 1e18, "s--", color="tab:blue", alpha=.7, label="control: n_i at the wall node"); ax1b.set_ylabel(r"$n_i$ [$10^{18}$ m$^{-3}$] (dashed)")
h1, l1 = axs[1].get_legend_handles_labels(); h2, l2 = ax1b.get_legend_handles_labels(); axs[1].legend(h1 + h2, l1 + l2, fontsize=8)
axs[1].set_title(f"electric field |grad phi| at the plate (ExB drift speed = |E|/B, B ~ 1.2 T) and ion density"); axs[1].set_ylabel("kV/m")
axs[2].plot(x, G["Ii"][j], "o-", color="tab:orange", label="GYRAZE: measured ion wall current"); axs[2].plot(x, C["Ii"][j], "o-", color="tab:blue", label="control: measured ion wall current"); axs[2].set_title(f"ion current per wall node (sheathdiag, g{STEP-20}-{STEP+20})"); axs[2].set_ylabel("A")
for ax in axs: ax.set_xlabel(r"wall node $\psi_N$"); ax.grid(alpha=.3)
axs[0].legend(fontsize=8); axs[2].legend(fontsize=8)
fig.suptitle("Ion current to the outer target, GYRAZE run vs control: the plate potential and its gradient are lower, the parallel flow is not"); fig.tight_layout(); fig.savefig(out, dpi=130); print("wrote", out)
