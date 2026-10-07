"""E x B drift near the divertor targets from xgc.2d potential (XGCa runs).

usage: python sheath_exb_layer.py <out_dir> <label>=<run_dir> [<label>=<run_dir> ...] [--steps 60,70,80]
Each run_dir needs xgc.mesh.bp, xgc.equil.bp, xgc.bfield.bp (first run only) and xgc.2d.<step>.bp.
Computes phi = pot0 + dpot (axisymmetric potential, V) averaged over the steps, the node-wise
least-squares gradient E = -grad(phi) in the (R,Z) plane, and v_ExB = E x B / B^2 (poloidal
components v_R = -E_Z B_phi/B^2, v_Z = E_R B_phi/B^2). Writes 2D zoom maps around the outer
target, the component of v_ExB normal to the plate, and profiles along the plate normal
(the "layer" over which the potential structure extends).
"""
import os, sys, json
import numpy as np, adios2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.tri import Triangulation, LinearTriInterpolator

argv = sys.argv[1:]
steps = (60, 70, 80)
if "--steps" in argv:
    k = argv.index("--steps"); steps = tuple(int(x) for x in argv[k + 1].split(",")); del argv[k:k + 2]
args = argv
out = os.path.abspath(args[0]); os.makedirs(out, exist_ok=True)
runs = [a.split("=", 1) for a in args[1:]]

def rd(p, v):
    with adios2.FileReader(p) as f: return np.squeeze(np.asarray(f.read(v)))

d0 = runs[0][1]
rz = rd(d0 + "/xgc.mesh.bp", "rz"); r, z = rz[:, 0], rz[:, 1]; cnct = rd(d0 + "/xgc.mesh.bp", "nd_connect_list")
psi = rd(d0 + "/xgc.mesh.bp", "psi"); wn = rd(d0 + "/xgc.mesh.bp", "grid_wall_nodes") - 1; region = rd(d0 + "/xgc.mesh.bp", "region")
x_psi = float(rd(d0 + "/xgc.equil.bp", "eq_x_psi")); x_r = float(rd(d0 + "/xgc.equil.bp", "eq_x_r")); x_z = float(rd(d0 + "/xgc.equil.bp", "eq_x_z"))
psin = psi / x_psi
B = rd(d0 + "/xgc.bfield.bp", "bfield"); BR, BZ, BP = B[0], B[1], B[2]; B2 = BR**2 + BZ**2 + BP**2
tri = Triangulation(r, z, cnct)
nnode = len(r)

# node least-squares gradient from triangle neighbours
nb = [set() for _ in range(nnode)]
for t in cnct:
    for a in t:
        for b in t:
            if a != b: nb[a].add(b)
def grad(f):
    gR = np.zeros(nnode); gZ = np.zeros(nnode)
    for i in range(nnode):
        j = np.fromiter(nb[i], int)
        if len(j) < 2: continue
        A = np.column_stack([r[j] - r[i], z[j] - z[i]]); b = f[j] - f[i]
        g, *_ = np.linalg.lstsq(A, b, rcond=None); gR[i], gZ[i] = g
    return gR, gZ

# outer target geometry: wall nodes below the X-point, R >= R_x, ordered by psiN; plate tangent/normal
ow = np.array([k for k in range(len(wn)) if z[wn[k]] < x_z and r[wn[k]] >= x_r]); ow = ow[np.argsort(psin[wn[ow]])]
Rw, Zw = r[wn[ow]], z[wn[ow]]
tR = np.gradient(Rw); tZ = np.gradient(Zw); tl = np.hypot(tR, tZ); tR /= tl; tZ /= tl
nR, nZ = -tZ, tR                      # normal rotated +90 deg: into the plasma for the floor-like outer plate
if np.mean(nZ) < 0: nR, nZ = -nR, -nZ

results = {}
for lbl, d in runs:
    phi = np.mean([rd(f"{d}/xgc.2d.{s:05d}.bp", "pot0") + rd(f"{d}/xgc.2d.{s:05d}.bp", "dpot") for s in steps], axis=0)
    ER, EZ = grad(phi); ER, EZ = -ER, -EZ
    vR = -EZ * BP / B2; vZ = ER * BP / B2
    vn_w = vR[wn[ow]] * nR + vZ[wn[ow]] * nZ           # normal component at the wall nodes (+ away from the plate)
    Et_w = -(ER[wn[ow]] * tR + EZ[wn[ow]] * tZ) * 0 + (ER[wn[ow]] * tR + EZ[wn[ow]] * tZ)  # tangential E (along increasing psiN)
    results[lbl] = dict(phi=phi, ER=ER, EZ=EZ, vR=vR, vZ=vZ, vn_w=vn_w, Et_w=Et_w)

# ---- figure 1: 2D maps near the outer target: phi, |v_ExB| and normal component
rmin, rmax, zmin, zmax = x_r - 0.02, 1.08, z.min() - 0.005, x_z + 0.03
sel = (r > rmin) & (r < rmax) & (z > zmin) & (z < zmax)
mask = np.any(~sel[cnct], axis=1); tri.set_mask(mask)
def decorate(ax):
    ax.tricontour(Triangulation(r, z, cnct), psin, levels=[1.0], colors="k", linewidths=0.8)
    ax.tricontour(Triangulation(r, z, cnct), psin, levels=[1.01, 1.02, 1.03, 1.04], colors="0.4", linewidths=0.4, linestyles="--")
    ax.plot(r[wn], z[wn], "-", color="0.2", lw=1.2); ax.plot(x_r, x_z, "kx")
    ax.set_xlim(rmin, rmax); ax.set_ylim(zmin, zmax); ax.set_aspect("equal"); ax.set_xlabel("R (m)"); ax.set_ylabel("Z (m)")
nrun = len(runs)
fig, axs = plt.subplots(nrun, 3, figsize=(16, 4.2 * nrun), squeeze=False)
vmax_phi = max(np.percentile(np.abs(results[l]["phi"][sel]), 99) for l, _ in runs)
for i, (lbl, _) in enumerate(runs):
    R_ = results[lbl]
    c = axs[i, 0].tripcolor(tri, R_["phi"], cmap="RdBu_r", vmin=-vmax_phi, vmax=vmax_phi); plt.colorbar(c, ax=axs[i, 0], label="phi = pot0+dpot (V)")
    vmag = np.hypot(R_["vR"], R_["vZ"]) / 1e3
    c = axs[i, 1].tripcolor(tri, vmag, cmap="magma", vmin=0, vmax=20); plt.colorbar(c, ax=axs[i, 1], label="|v_ExB,pol| (km/s)")
    sk = sel & (np.arange(nnode) % 3 == 0)
    axs[i, 1].quiver(r[sk], z[sk], R_["vR"][sk], R_["vZ"][sk], color="w", scale=4e5, width=0.002)
    # normal component field: project on the local plate normal (nearest wall node)
    from scipy.spatial import cKDTree
    kd = cKDTree(np.column_stack([Rw, Zw])); dist, j = kd.query(np.column_stack([r, z]))
    vn = R_["vR"] * nR[j] + R_["vZ"] * nZ[j]
    c = axs[i, 2].tripcolor(tri, vn / 1e3, cmap="seismic", vmin=-15, vmax=15); plt.colorbar(c, ax=axs[i, 2], label="v_ExB . n_plate (km/s, + away from plate)")
    for a, t in zip(axs[i], (f"{lbl}: potential", f"{lbl}: E x B drift (poloidal)", f"{lbl}: E x B normal to the outer plate")):
        decorate(a); a.set_title(t, fontsize=10)
fig.suptitle(f"Outer divertor target, gstep {steps} average; black = separatrix, dashed = psi_N 1.01-1.04")
fig.tight_layout(); fig.savefig(out + "/exb_outer_target_maps.png", dpi=130); plt.close(fig)

# ---- figure 2: along the plate and across the layer
psiw = psin[wn[ow]]
fig, axs = plt.subplots(1, 3, figsize=(18, 4.8))
for lbl, _ in runs:
    R_ = results[lbl]
    axs[0].plot(psiw, R_["phi"][wn[ow]], "-o", ms=3, label=lbl)
    axs[1].plot(psiw, R_["vn_w"] / 1e3, "-o", ms=3, label=lbl)
axs[0].set_ylabel("phi at the wall node (V)"); axs[1].set_ylabel("v_ExB . n at the wall (km/s, + away)")
axs[1].axhline(0, color="k", lw=0.8)
cs = np.sqrt((45 + 100) * 1.602e-19 / (2 * 1.6726e-27)); axs[1].axhspan(-cs * np.sin(np.radians(2)) / 1e3, cs * np.sin(np.radians(2)) / 1e3, color="0.85", label="c_s sin(2 deg) parallel-streaming normal speed")
for a in axs[:2]: a.set_xlim(0.98, 1.08); a.set_xlabel("psi_N along the outer plate"); a.grid(alpha=0.3); a.axvline(1, color="k", ls=":")
axs[0].legend(fontsize=8); axs[1].legend(fontsize=7)
# layer profiles: along the plate normal at psiN = 1.015 (dip) and 1.035 (peak), first run
lbl0 = runs[0][0]; R_ = results[lbl0]
interp_phi = LinearTriInterpolator(Triangulation(r, z, cnct), R_["phi"])
interp_vn = LinearTriInterpolator(Triangulation(r, z, cnct), R_["vR"] * nR[j] + R_["vZ"] * nZ[j])
dists = np.linspace(0, 0.03, 61)
layer = {}
for target_psi, col in ((1.015, "tab:red"), (1.035, "tab:blue"), (1.005, "tab:green")):
    k = np.argmin(np.abs(psiw - target_psi))
    Rl = Rw[k] + dists * nR[k]; Zl = Zw[k] + dists * nZ[k]
    ph = np.array(interp_phi(Rl, Zl)); vn_ = np.array(interp_vn(Rl, Zl)) / 1e3
    axs[2].plot(dists * 100, ph, "-", color=col, label=f"phi, psi_N={psiw[k]:.3f}")
    axs[2].plot(dists * 100, vn_ * 10, "--", color=col, label=f"10 x v_n (km/s), psi_N={psiw[k]:.3f}")
    layer[f"{psiw[k]:.3f}"] = dict(phi=ph.tolist(), vn_kms=vn_.tolist())
axs[2].set_xlabel("distance from the plate along its normal (cm)"); axs[2].set_ylabel("V  /  10 x km/s"); axs[2].grid(alpha=0.3); axs[2].legend(fontsize=7)
axs[2].set_title(f"{lbl0}: layer structure", fontsize=10)
fig.suptitle(f"Outer target, gstep {steps} average")
fig.tight_layout(); fig.savefig(out + "/exb_outer_target_profiles.png", dpi=130); plt.close(fig)
json.dump({"steps": steps, "dist_cm": (dists * 100).tolist(), "layer_" + lbl0: layer,
           "wall": {lbl: dict(psin=psiw.tolist(), phi=results[lbl]["phi"][wn[ow]].tolist(), vn_kms=(results[lbl]["vn_w"] / 1e3).tolist()) for lbl, _ in runs}},
          open(out + "/exb_outer_target.json", "w"))
print("wrote", out)
