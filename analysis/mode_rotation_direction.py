"""Mode rotation direction (electron vs ion diamagnetic) from XGC xgc.3d.*.bp files,
radially resolved at the outboard midplane, in the lab frame and in the E x B frame.

Method
  c_n(t, node) = sum_j dpot(j, node) exp(-i n phi_j)  over the planes of the wedge.
  With dpot ~ cos(n phi - omega t):  arg c_n = -omega t,  omega_lab = -d(arg c_n)/dt.
  sign(omega/n) > 0  <=>  propagation toward +phi.  XGC phi is counter-clockwise seen
  from above, (R, phi, Z) right-handed; the file's own `phi` array is used.
  Doppler shift: a field-aligned pattern convected by v_E = E x B / B^2 with E = E_R R_hat
  at the outboard midplane appears to rotate toroidally at
      omega_E = -E_R / (R B_Z) = (d pot0/dR) / (R B_Z),
  so omega_plasma = omega_lab - n omega_E.  pot0 comes from the same 3d files.
  Electron diamagnetic toroidal direction at the outboard midplane (inward grad p):
      sign(v_*e . phi_hat) = sign(B_Z)   (counter to Ip);  ion direction = -sign(B_Z).
  B_Z is read from xgc.bfield.bp, so no sign convention is assumed.
  Independent check: plane-shift cross-correlation (does the pattern move to higher
  plane index, i.e. +phi, between consecutive outputs?).

Usage: python mode_rotation_direction.py <run_dir> [--psi 0.3 0.95] [--nbins 13]
       [--dz 0.03] [--every k] [--late 0.33] [--out result.json]
Runs on a login node: file access only.
"""
import argparse, glob, json, sys
import numpy as np
from adios2 import FileReader


def read(path, *names):
    with FileReader(path) as f:
        out = []
        for nm in names:
            try:
                out.append(f.read(nm))
            except Exception:
                out.append(None)
    return out if len(out) > 1 else out[0]


def wmean(x, w):
    s = w.sum()
    return float((x * w).sum() / s) if s > 0 else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--psi", nargs=2, type=float, default=[0.3, 0.95])
    ap.add_argument("--nbins", type=int, default=13)
    ap.add_argument("--dz", type=float, default=0.03)
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--max-files", type=int, default=400)
    ap.add_argument("--late", type=float, default=0.33, help="fraction of the record used (from the end)")
    ap.add_argument("--var", default="dpot")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = a.run_dir.rstrip("/")

    rz, psi, qsafety, psi_surf = read(f"{d}/xgc.mesh.bp", "rz", "psi", "qsafety", "psi_surf")
    axis_r, axis_z, psi_x, bp_sign, bt_sign = read(
        f"{d}/xgc.equil.bp", "eq_axis_r", "eq_axis_z", "eq_x_psi", "bp_sign", "bt_sign")
    axis_r, axis_z, psi_x = float(axis_r), float(axis_z), float(psi_x)
    bfield = read(f"{d}/xgc.bfield.bp", "bfield")  # (3, nnode): (B_R, B_Z, B_phi) checked below
    R, Z = rz[:, 0], rz[:, 1]
    psin = psi / psi_x

    sel = np.where((np.abs(Z - axis_z) < a.dz) & (R > axis_r)
                   & (psin > a.psi[0]) & (psin < a.psi[1]))[0]
    if len(sel) < 10:
        sys.exit(f"only {len(sel)} midplane nodes selected")
    comp = bfield[:, sel]
    bphi_idx = int(np.argmax(np.abs(comp).mean(axis=1)))
    others = [i for i in range(3) if i != bphi_idx]
    bz_idx = max(others, key=lambda i: abs(np.mean(np.sign(comp[i]))))
    br_idx = [i for i in others if i != bz_idx][0]
    Bz, Bphi = bfield[bz_idx], bfield[bphi_idx]
    Bmag = np.sqrt((bfield ** 2).sum(axis=0))
    bz_sign = float(np.sign(np.mean(Bz[sel])))
    electron_dir = bz_sign
    ion_dir = -bz_sign

    files = sorted(glob.glob(f"{d}/xgc.3d.[0-9]*.bp"))
    if not files:
        sys.exit("no xgc.3d files")
    files = files[::a.every][-a.max_files:]
    phi = read(files[0], "phi")
    nphi = len(phi)
    assert np.all(np.diff(phi) > 0), "plane angles not increasing"
    dphi = float(phi[1] - phi[0])
    wedge = int(round(2 * np.pi / (dphi * nphi)))
    ns = [wedge * k for k in range(1, nphi // 2)]  # skip Nyquist

    times, C, V, P0 = [], [], [], []
    for fn in files:
        with FileReader(fn) as f:
            t = float(f.read("time"))
            v = f.read(a.var)[:, sel]
            p0 = f.read("pot0")[sel]
        v = v - v.mean(axis=0, keepdims=True)
        cn = np.array([(v * np.exp(-1j * n * phi)[:, None]).sum(axis=0) for n in ns])
        times.append(t); C.append(cn); V.append(v); P0.append(p0)
    times = np.array(times); C = np.array(C); V = np.array(V); P0 = np.array(P0)
    nt = len(times)
    if nt < 3:
        sys.exit("need at least 3 snapshots")
    dts = np.diff(times)
    dt_med = float(np.median(dts))
    good_int = dts < 3 * dt_med  # skip restart gaps
    k0 = max(1, int(nt * (1 - a.late)))
    late_int = np.arange(nt - 1) >= k0
    use = good_int & late_int
    if use.sum() < 1:
        use = good_int

    # psi bins
    edges = np.linspace(a.psi[0], a.psi[1], a.nbins + 1)
    ps = psin[sel]; Rs = R[sel]
    bins = np.digitize(ps, edges) - 1

    # pot0 radial derivative on the midplane (late-time mean), per bin
    p0_late = P0[k0:].mean(axis=0)
    o = np.argsort(Rs)
    Rb, Pb = [], []
    for b in range(a.nbins):
        m = bins == b
        if m.sum() >= 2:
            Rb.append(Rs[m].mean()); Pb.append(p0_late[m].mean())
    Rb, Pb = np.array(Rb), np.array(Pb)
    dp0dR_fn = (lambda r: np.interp(r, Rb, np.gradient(Pb, Rb))) if len(Rb) >= 3 else (lambda r: np.nan)

    # profiles for omega_* estimates (full 1d moments, late time)
    prof = {}
    try:
        with FileReader(f"{d}/xgc.oneddiag.bp") as f:
            names = set(f.available_variables().keys())
            def last(nm):
                v = np.asarray(f.read(nm)); return v[-1] if v.ndim == 2 else v
            p1 = last("psi")
            if p1.max() > 2: p1 = p1 / psi_x
            for sp in ("e", "i"):
                if f"{sp}_gc_density_1d" in names and f"{sp}_perp_temperature_1d" in names:
                    prof[sp] = (p1, last(f"{sp}_gc_density_1d"), last(f"{sp}_perp_temperature_1d"))
    except Exception as e:
        prof["error"] = repr(e)

    def omega_star(sp, psin0, n, R0):
        if sp not in prof:
            return None, None
        p1, nn, tt = prof[sp]
        gd = (nn > 0) & (tt > 0) & np.isfinite(nn) & np.isfinite(tt)
        p1, nn, tt = p1[gd], nn[gd], tt[gd]
        Rp = np.interp(p1, ps[o], Rs[o])
        dn, dT = np.gradient(nn, Rp), np.gradient(tt, Rp)
        j = int(np.argmin(np.abs(p1 - psin0)))
        invLn, invLT = -dn[j] / nn[j], -dT[j] / tt[j]
        r = Rp[j] - axis_r
        q = float(np.interp(psin0, psi_surf / psi_x, np.abs(qsafety)))
        B0 = float(np.interp(psin0, ps[o], Bmag[sel][o]))
        ktheta = n * q / r
        w_n = ktheta * tt[j] / B0 * invLn       # |omega_*| density part, rad/s
        w_p = ktheta * tt[j] / B0 * (invLn + invLT)
        return float(w_n), float(w_p)

    res = {"run_dir": d, "n_files": nt, "t0_s": float(times[0]), "t1_s": float(times[-1]),
           "dt_out_s": dt_med, "n_intervals_used": int(use.sum()), "nphi": nphi, "wedge_n": wedge,
           "nsel": int(len(sel)), "psi_band": a.psi, "bp_sign": float(bp_sign), "bt_sign": float(bt_sign),
           "B_component_index": {"R": br_idx, "Z": bz_idx, "phi": bphi_idx},
           "Bz_sign_outboard_midplane": bz_sign, "Bphi_sign": float(np.sign(np.mean(Bphi[sel]))),
           "electron_diamagnetic_toroidal_sign": electron_dir, "ion_diamagnetic_toroidal_sign": ion_dir,
           "convention": "omega_lab=-d(arg c_n)/dt; tor sign=sign(omega/n); +1 = +phi (CCW from above); "
                         "omega_E=(dpot0/dR)/(R B_Z); omega_pl=omega_lab-n*omega_E",
           "bins": []}

    for b in range(a.nbins):
        m = np.where(bins == b)[0]
        if len(m) < 2:
            continue
        pc = 0.5 * (edges[b] + edges[b + 1]); Rc = float(Rs[m].mean())
        Bz_c = float(Bz[sel][m].mean())
        omega_E = float(dp0dR_fn(Rc) / (Rc * Bz_c))
        E_R = float(-dp0dR_fn(Rc))
        # plane-shift cross-correlation over all n (convention-free direction check)
        v1, v0 = V[1:][use][:, :, m], V[:-1][use][:, :, m]
        splus = float((v1 * np.roll(v0, 1, axis=1)).sum())   # pattern moved to higher j (+phi)
        sminus = float((v1 * np.roll(v0, -1, axis=1)).sum())
        xcorr_sign = float(np.sign(splus - sminus))
        entry = {"psin": [float(edges[b]), float(edges[b + 1])], "R_m": Rc, "n_nodes": int(len(m)),
                 "Bz_T": Bz_c, "E_R_V_m": E_R, "omega_E_rad_s": omega_E,
                 "xcorr_shift_sign": xcorr_sign, "xcorr_asym": float((splus - sminus) / max(abs(splus) + abs(sminus), 1e-300)),
                 "modes": []}
        for i, n in enumerate(ns):
            c = C[:, i, :][:, m]
            amp2 = np.abs(c) ** 2
            dth = np.angle(c[1:] * np.conj(c[:-1]))          # (nt-1, nodes)
            wt = np.sqrt(amp2[1:] * amp2[:-1])
            dth_rate = dth / dts[:, None]                      # rad/s per node per interval
            W = wt[use]; D = dth_rate[use]
            dthdt = wmean(D, W)
            frac_pos = wmean((D > 0).astype(float), W)
            omega_lab = -dthdt
            omega_pl = omega_lab - n * omega_E
            A_late = float(np.sqrt(amp2[k0:].sum(axis=1)).mean())
            lab_sign = float(np.sign(omega_lab / n)) if omega_lab != 0 else 0.0
            pl_sign = float(np.sign(omega_pl / n)) if np.isfinite(omega_pl) and omega_pl != 0 else 0.0
            lab_dir = {electron_dir: "electron", ion_dir: "ion"}.get(lab_sign, "undetermined")
            pl_dir = {electron_dir: "electron", ion_dir: "ion"}.get(pl_sign, "undetermined")
            wse = omega_star("e", pc, n, Rc); wsi = omega_star("i", pc, n, Rc)
            entry["modes"].append({
                "n": n, "omega_lab_rad_s": omega_lab, "omega_lab_kHz": omega_lab / 2 / np.pi / 1e3,
                "doppler_n_omega_E_rad_s": n * omega_E,
                "omega_plasma_rad_s": omega_pl, "omega_plasma_kHz": omega_pl / 2 / np.pi / 1e3,
                "mean_dtheta_per_output_rad": float(dthdt * dt_med),
                "frac_weight_dtheta_positive": frac_pos,
                "lab_direction": lab_dir, "plasma_direction": pl_dir,
                "amplitude_late": A_late,
                "omega_star_e_abs": wse[0], "omega_star_pe_abs": wse[1],
                "omega_star_i_abs": wsi[0], "omega_star_pi_abs": wsi[1],
            })
        res["bins"].append(entry)

    out = a.out or f"{d}/mode_rotation_direction.json"
    with open(out, "w") as f:
        json.dump(res, f, indent=1)
    print(f"# {d}: files={nt} t={times[0]*1e6:.2f}..{times[-1]*1e6:.2f} us dt_out={dt_med:.3e}s nphi={nphi} wedge={wedge} "
          f"Bz_sign={bz_sign:+.0f} electron_dir={electron_dir:+.0f} ion_dir={ion_dir:+.0f} intervals_used={use.sum()}")
    print("# psin     R     E_R[kV/m] omega_E[krad/s] xcorr |  n  A_late   dth/out  frac+  w_lab[kHz]  nW_E[kHz]  w_pl[kHz]  lab   plasma  |w*e| |w*pe| |w*i| |w*pi| [kHz]")
    for e in res["bins"]:
        for k, mm in enumerate(e["modes"]):
            head = (f"{e['psin'][0]:.2f}-{e['psin'][1]:.2f} {e['R_m']:.3f} {e['E_R_V_m']/1e3:+8.2f} {e['omega_E_rad_s']/1e3:+9.2f}  {e['xcorr_shift_sign']:+.0f}  "
                    if k == 0 else " " * 44)
            f2 = lambda x: f"{x/2/np.pi/1e3:6.0f}" if x is not None and np.isfinite(x) else "   nan"
            print(head + f"| {mm['n']:3d} {mm['amplitude_late']:8.1e} {mm['mean_dtheta_per_output_rad']:+7.3f} {mm['frac_weight_dtheta_positive']:5.2f} "
                  f"{mm['omega_lab_kHz']:+9.1f} {mm['doppler_n_omega_E_rad_s']/2/np.pi/1e3:+9.1f} {mm['omega_plasma_kHz']:+9.1f}  "
                  f"{mm['lab_direction'][:4]:4s}  {mm['plasma_direction'][:4]:4s}  "
                  f"{f2(mm['omega_star_e_abs'])} {f2(mm['omega_star_pe_abs'])} {f2(mm['omega_star_i_abs'])} {f2(mm['omega_star_pi_abs'])}")


if __name__ == "__main__":
    main()
