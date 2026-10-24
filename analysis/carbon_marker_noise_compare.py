#!/usr/bin/env python3
"""Compare particle-noise signatures of XGCa runs that differ in the carbon marker type.

Runs on the HPC login node (adios2 + numpy + matplotlib) and reduces the output of
several XGCa runs to a small JSON plus a few PNG figures, so nothing large has to
leave the machine.  Written for the d0116pe / d0117pe / d0118pe triplet (TCV NT,
e + D + C6+, carbon TotalF vs FullF, with and without the f0g update) but any set of
XGCa runs with the same species layout works.

Metrics
  oneddiag  per species: flux-surface-averaged density (f0 + df, and the particle
            moment alone), parallel flow; pot00_1d.  "noise" = rms of the residual
            after a centred moving average in time, relative to the mean; "drift" =
            change between the first and last window.
  2d / f2d  spatial roughness = rms of (value - mean of mesh neighbours) in psi
            bands, normalised; temporal jitter = rms of the change between
            consecutive outputs; particle weight variance per species vs time.
  log       mixed-marker warning, seconds per step (timing_all.txt_stats MAIN_LOOP),
            last gstep, "XGC finished".

Usage
  carbon_marker_noise_compare.py --out OUTDIR LABEL=RUN_DIR [LABEL=RUN_DIR ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import numpy as np

try:
    import adios2
except ImportError:  # pragma: no cover
    sys.exit("adios2 python module required (use the analysis python of the machine)")

SPECIES = {"e": "e_", "D": "i_", "C": "i2"}  # oneddiag / 2d / f2d name prefixes
BANDS = [(0.2, 0.6), (0.6, 0.9), (0.9, 1.0), (1.0, 1.05)]
PSI_PROBES = [0.5, 0.8, 0.95]


# ----------------------------------------------------------------------------- io
def read_steps(path: str, names: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    with adios2.FileReader(path) as fr:
        av = fr.available_variables()
        n = fr.num_steps()
        for name in names:
            if name not in av:
                continue
            arr = np.asarray(fr.read(name, step_selection=[0, n]))
            # adios2 returns the steps of a 1-D variable flattened: restore (steps, len)
            shape = av[name].get("Shape", "")
            if shape and arr.ndim == 1:
                per_step = int(np.prod([int(x) for x in shape.split(",") if x.strip()]))
                if per_step and arr.size == per_step * n:
                    arr = arr.reshape(n, per_step)
            elif not shape and arr.ndim == 1 and arr.size == n:
                pass  # scalar per step
            out[name] = arr
    return out


def read_one(path: str, names: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    with adios2.FileReader(path) as fr:
        av = fr.available_variables()
        for name in names:
            if name in av:
                out[name] = np.squeeze(np.asarray(fr.read(name)))
    return out


# ------------------------------------------------------------------------- helpers
def moving_average(x: np.ndarray, w: int) -> np.ndarray:
    """Centred moving average along axis 0 with edge padding."""
    if w < 2 or x.shape[0] < w:
        return x.copy()
    pad = w // 2
    xp = np.concatenate([np.repeat(x[:1], pad, axis=0), x, np.repeat(x[-1:], pad, axis=0)], axis=0)
    k = np.ones(w) / w
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid")[: x.shape[0]], 0, xp)


def rel_noise(q: np.ndarray, window: int = 5) -> np.ndarray:
    """Relative high-frequency noise per psi bin of a (time, psi) series."""
    if q.shape[0] < window + 2:
        return np.full(q.shape[1], np.nan)
    resid = q - moving_average(q, window)
    scale = np.abs(np.mean(q, axis=0))
    scale = np.where(scale > 0, scale, np.nan)
    return np.sqrt(np.mean(resid**2, axis=0)) / scale


def abs_noise(q: np.ndarray, window: int = 5) -> np.ndarray:
    if q.shape[0] < window + 2:
        return np.full(q.shape[1], np.nan)
    resid = q - moving_average(q, window)
    return np.sqrt(np.mean(resid**2, axis=0))


def band_mask(psi: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return (psi >= lo) & (psi < hi)


def nearest_index(psi: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(psi - value)))


def neighbour_mean(values: np.ndarray, tri: np.ndarray, nnode: int) -> np.ndarray:
    """Mean of the mesh neighbours of every node (triangle connectivity)."""
    acc = np.zeros(nnode)
    cnt = np.zeros(nnode)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        i, j = tri[:, a], tri[:, b]
        np.add.at(acc, i, values[j]); np.add.at(cnt, i, 1)
        np.add.at(acc, j, values[i]); np.add.at(cnt, j, 1)
    cnt[cnt == 0] = 1
    return acc / cnt


def roughness(values: np.ndarray, mesh: dict, scale: np.ndarray | float, lo: float, hi: float) -> float:
    """rms of (value - neighbour mean) / scale inside a psi band, interior nodes only."""
    m = band_mask(mesh["psin"], lo, hi) & (mesh["region"] <= 2)
    if not np.any(m):
        return float("nan")
    r = (values - mesh["nbr_mean"](values))
    s = scale if np.isscalar(scale) else np.where(np.abs(scale) > 0, np.abs(scale), np.nan)
    return float(np.sqrt(np.nanmean((r[m] / (s if np.isscalar(s) else s[m])) ** 2)))


def band_rms(values: np.ndarray, mesh: dict, scale, lo: float, hi: float) -> float:
    m = band_mask(mesh["psin"], lo, hi) & (mesh["region"] <= 2)
    if not np.any(m):
        return float("nan")
    s = scale if np.isscalar(scale) else np.where(np.abs(scale) > 0, np.abs(scale), np.nan)
    return float(np.sqrt(np.nanmean((values[m] / (s if np.isscalar(s) else s[m])) ** 2)))


# ------------------------------------------------------------------------- per run
def load_mesh(run_dir: str) -> dict:
    m = read_one(f"{run_dir}/xgc.mesh.bp", ["psi", "rz", "theta", "region", "nd_connect_list", "node_vol"])
    psi = m["psi"]
    tri = m["nd_connect_list"].astype(int)
    if tri.min() == 1:
        tri = tri - 1
    nnode = psi.shape[0]
    # psi normalisation from xgc.units.bp if present, else from the separatrix region code
    psix = None
    try:
        u = read_one(f"{run_dir}/xgc.units.bp", ["psi_x"])
        psix = float(u.get("psi_x"))
    except Exception:
        psix = None
    if not psix:
        psix = float(np.max(psi[m["region"] == 1])) if np.any(m["region"] == 1) else float(np.max(psi))
    mesh = {
        "psin": psi / psix,
        "rz": m["rz"],
        "theta": m["theta"],
        "region": m["region"],
        "tri": tri,
        "nnode": nnode,
        "psix": psix,
    }
    mesh["nbr_mean"] = lambda v, _tri=tri, _n=nnode: neighbour_mean(v, _tri, _n)
    f0 = read_one(f"{run_dir}/xgc.f0.mesh.bp", ["f0_den", "f0_T_ev"])
    mesh["f0_den"] = f0.get("f0_den")  # (nsp, nnode): e, D, C
    mesh["f0_T_ev"] = f0.get("f0_T_ev")
    return mesh


def parse_log(run_dir: str) -> dict:
    info: dict = {}
    logs = sorted(glob.glob(f"{run_dir}/slurm-*.out"))
    if logs:
        text = open(logs[-1], errors="replace").read()
        info["log"] = os.path.basename(logs[-1])
        info["mixed_marker_warning"] = "combination of kinetic MarkerType" in text
        info["finished"] = "XGC finished" in text
        steps = re.findall(r"istep, gstep, trigger, ratio, # of ion\s+(\d+)\s+(\d+)", text)
        if steps:
            info["last_gstep_in_log"] = int(steps[-1][1])
        mem = re.findall(r"CPU memory usage at the beginning of time step: Min/Avg/Max used = ([\d.]+)/([\d.]+)/([\d.]+)GB", text)
        if mem:
            info["cpu_mem_gb_max_last"] = float(mem[-1][2])
        errs = [ln.strip() for ln in text.splitlines() if re.search(r"\bERROR\b|Error:|CUDA|NaN", ln)]
        info["error_lines"] = errs[:5]
    stats = f"{run_dir}/timing_all.txt_stats"
    if os.path.exists(stats):
        for ln in open(stats, errors="replace"):
            if ln.startswith('"MAIN_LOOP"'):
                parts = ln.replace('"', "").split()
                try:
                    nprocs = float(parts[2]); count = float(parts[4]); wallmax = float(parts[6])
                    nsteps = count / nprocs
                    info["main_loop_s_per_step"] = wallmax / nsteps if nsteps else None
                    info["main_loop_steps"] = int(nsteps)
                except (ValueError, IndexError):
                    pass
                break
    return info


def analyse_oneddiag(run_dir: str, psix: float | None = None) -> dict:
    names = ["time", "gstep", "psi", "psi00", "pot00_1d", "eden00_1d", "iden00_1d", "cden00_1d"]
    for pre in SPECIES.values():
        for q in ["gc_density_1d", "gc_density_df_1d", "parallel_flow_1d", "parallel_flow_df_1d",
                  "marker_density_1d", "perp_temperature_df_1d"]:
            names.append(pre + q)
    d = read_steps(f"{run_dir}/xgc.oneddiag.bp", names)
    out: dict = {}
    if "psi" not in d:
        return out
    psi = d["psi"][-1] if d["psi"].ndim == 2 else d["psi"]
    gstep = d["gstep"].ravel()
    out["samples"] = int(len(gstep)); out["gstep_first"] = int(gstep[0]); out["gstep_last"] = int(gstep[-1])
    out["psi"] = psi.tolist()
    out["species"] = {}
    for sp, pre in SPECIES.items():
        rec: dict = {}
        for key, var in (("density_total", pre + "gc_density_df_1d"), ("density_particle", pre + "gc_density_1d"),
                         ("upar_total", pre + "parallel_flow_df_1d"), ("upar_particle", pre + "parallel_flow_1d")):
            if var not in d:
                continue
            q = d[var]
            if key.startswith("density"):
                nz = rel_noise(q)
                rec[key + "_rel_noise_profile"] = nz.tolist()
                core = band_mask(psi, 0.3, 0.95)
                rec[key + "_rel_noise_mean_0.3-0.95"] = float(np.nanmean(nz[core]))
                w = min(5, q.shape[0] // 2) or 1
                rec[key + "_rel_drift_profile"] = ((np.mean(q[-w:], axis=0) - np.mean(q[:w], axis=0)) / np.abs(np.mean(q[:w], axis=0))).tolist()
                rec[key + "_final_profile"] = np.mean(q[-w:], axis=0).tolist()
                rec[key + "_initial_profile"] = np.mean(q[:w], axis=0).tolist()
            else:
                rec[key + "_abs_noise_profile_m_s"] = abs_noise(q).tolist()
                rec[key + "_traces_m_s"] = {str(p): q[:, nearest_index(psi, p)].tolist() for p in PSI_PROBES}
                w = min(5, q.shape[0] // 2) or 1
                rec[key + "_final_profile_m_s"] = np.mean(q[-w:], axis=0).tolist()
        # A full-f species has no f0, so its f0+df (_df_1d) moments come out as zeros; its
        # marker (_1d) moments carry the whole distribution and are the ones to compare.
        df_var = pre + "gc_density_df_1d"
        rec["df_moments_zero"] = bool(df_var in d and not np.any(d[df_var]))
        rec["best"] = "particle" if rec["df_moments_zero"] else "total"
        out["species"][sp] = rec
    if "pot00_1d" in d:
        p = d["pot00_1d"]; psi00 = d["psi00"][-1] if d["psi00"].ndim == 2 else d["psi00"]
        # psi00 is in Wb/rad; normalise to the separatrix so the probes mean psi_n
        if psix:
            psi00 = psi00/psix
        out["pot00"] = {
            "psi00": psi00.tolist(),
            "abs_noise_profile_V": abs_noise(p).tolist(),
            "traces_V": {str(x): p[:, nearest_index(psi00, x)].tolist() for x in PSI_PROBES},
            "final_profile_V": np.mean(p[-5:], axis=0).tolist(),
        }
    for key in ("eden00_1d", "iden00_1d", "cden00_1d"):
        if key in d:
            q = d[key]
            out[key] = {"final_profile": np.mean(q[-5:], axis=0).tolist(), "abs_noise_profile": abs_noise(q).tolist()}
    out["time"] = d["time"].ravel().tolist()
    out["gstep"] = gstep.tolist()
    return out


def analyse_fields(run_dir: str, mesh: dict) -> dict:
    out: dict = {"steps": [], "pot0": {}, "dpot": {}, "weights": {}, "f2d": {}}
    files2d = sorted(glob.glob(f"{run_dir}/xgc.2d.*.bp"))
    filesf2 = sorted(glob.glob(f"{run_dir}/xgc.f2d.*.bp"))
    te = mesh["f0_T_ev"][0] if mesh.get("f0_T_ev") is not None else 1.0
    nC0 = mesh["f0_den"][2] if mesh.get("f0_den") is not None and mesh["f0_den"].shape[0] >= 3 else None
    nD0 = mesh["f0_den"][1] if mesh.get("f0_den") is not None else None
    prev: dict = {}
    # f2d holds moments of the f0 grid only; a full-f carbon has none there. Its density is then
    # marker density x mean marker weight (the weight is w0 alone for full-f markers).
    carbon_in_f2d = bool(filesf2) and "i2den" in read_one(filesf2[0], ["i2den"])
    out["f2d"]["nC_source"] = "f2d i2den (f0 + delta-f)" if carbon_in_f2d else "2d i2marker_den*i2mean_weight (full-f markers)"
    prev_nC = None
    for f in files2d:
        d = read_one(f, ["gstep", "pot0", "dpot", "iden", "eden", "e_mean_weight", "e_weight_variance",
                        "i_mean_weight", "i_weight_variance", "i2mean_weight", "i2weight_variance",
                        "i2marker_den", "i_marker_den", "e_marker_den"])
        g = int(d["gstep"])
        out["steps"].append(g)
        for name in ("pot0", "dpot"):
            if name not in d:
                continue
            v = d[name]
            rec = out[name].setdefault("roughness_over_Te", {})
            jit = out[name].setdefault("jitter_over_Te", {})
            for lo, hi in BANDS:
                rec.setdefault(f"{lo}-{hi}", []).append(roughness(v, mesh, te, lo, hi))
                if name in prev:
                    jit.setdefault(f"{lo}-{hi}", []).append(band_rms(v - prev[name], mesh, te, lo, hi))
            prev[name] = v
        for sp, pre in (("e", "e_"), ("D", "i_"), ("C", "i2")):
            for q in ("mean_weight", "weight_variance", "marker_den"):
                key = pre + q
                if key in d:
                    m = (mesh["psin"] < 1.0) & (mesh["region"] == 1)
                    out["weights"].setdefault(f"{sp}_{q}_core_mean", []).append(float(np.mean(d[key][m])))
                    out["weights"].setdefault(f"{sp}_{q}_core_max", []).append(float(np.max(np.abs(d[key][m]))))
        if not carbon_in_f2d and nC0 is not None and "i2marker_den" in d and "i2mean_weight" in d:
            nC = d["i2marker_den"]*d["i2mean_weight"]
            rec = out["f2d"].setdefault("nC_over_nC0", {})
            out["f2d"].setdefault("steps", []).append(g)
            for lo, hi in BANDS:
                rec.setdefault(f"roughness_{lo}-{hi}", []).append(roughness(nC, mesh, nC0, lo, hi))
                if prev_nC is not None:
                    rec.setdefault(f"jitter_{lo}-{hi}", []).append(band_rms(nC - prev_nC, mesh, nC0, lo, hi))
            prev_nC = nC
            mid = (np.abs(mesh["theta"]) < 0.15) & (mesh["rz"][:, 0] > np.median(mesh["rz"][:, 0]))
            order = np.argsort(mesh["psin"][mid])
            out["f2d"]["midplane_last"] = {"psin": mesh["psin"][mid][order].tolist(),
                                           "nC_over_nC0": (nC[mid][order]/np.where(nC0[mid][order] > 0, nC0[mid][order], np.nan)).tolist(),
                                           "gstep": g}
        if "iden" in d and nD0 is not None:
            rec = out.setdefault("iden_over_nD0", {})
            for lo, hi in BANDS:
                rec.setdefault(f"roughness_{lo}-{hi}", []).append(roughness(d["iden"], mesh, nD0, lo, hi))
    prevf: dict = {}
    if carbon_in_f2d:
        out["f2d"]["steps"] = []
    for f in filesf2:
        d = read_one(f, ["gstep", "i2den", "i_den", "e_den", "i2u_para", "i2_f0_delta_n", "i_f0_delta_n", "e_f0_delta_n"])
        g = int(d["gstep"])
        if carbon_in_f2d:
            out["f2d"]["steps"].append(g)
        if "i2den" in d and nC0 is not None:
            rec = out["f2d"].setdefault("nC_over_nC0", {})
            for lo, hi in BANDS:
                rec.setdefault(f"roughness_{lo}-{hi}", []).append(roughness(d["i2den"], mesh, nC0, lo, hi))
                if "i2den" in prevf:
                    rec.setdefault(f"jitter_{lo}-{hi}", []).append(band_rms(d["i2den"] - prevf["i2den"], mesh, nC0, lo, hi))
            # outboard-midplane sample for the figure
            mid = (np.abs(mesh["theta"]) < 0.15) & (mesh["rz"][:, 0] > np.median(mesh["rz"][:, 0]))
            order = np.argsort(mesh["psin"][mid])
            out["f2d"]["midplane_last"] = {
                "psin": mesh["psin"][mid][order].tolist(),
                "nC_over_nC0": (d["i2den"][mid][order] / np.where(nC0[mid][order] > 0, nC0[mid][order], np.nan)).tolist(),
                "gstep": g,
            }
            prevf["i2den"] = d["i2den"]
        if "i_den" in d and nD0 is not None:
            rec = out["f2d"].setdefault("nD_over_nD0", {})
            for lo, hi in BANDS:
                rec.setdefault(f"roughness_{lo}-{hi}", []).append(roughness(d["i_den"], mesh, nD0, lo, hi))
        for sp, key in (("C", "i2_f0_delta_n"), ("D", "i_f0_delta_n"), ("e", "e_f0_delta_n")):
            if key in d:
                ref = {"C": nC0, "D": nD0, "e": mesh["f0_den"][0] if mesh.get("f0_den") is not None else None}[sp]
                if ref is not None:
                    m = (mesh["psin"] < 0.95) & (mesh["region"] == 1)
                    out["f2d"].setdefault(f"{sp}_f0_delta_n_over_n0_core_rms", []).append(
                        float(np.sqrt(np.mean((d[key][m] / ref[m]) ** 2))))
    if files2d:
        d = read_one(files2d[-1], ["pot0"])
        mid = (np.abs(mesh["theta"]) < 0.15) & (mesh["rz"][:, 0] > np.median(mesh["rz"][:, 0]))
        order = np.argsort(mesh["psin"][mid])
        out["pot0"]["midplane_last"] = {"psin": mesh["psin"][mid][order].tolist(), "pot0_V": d["pot0"][mid][order].tolist(),
                                        "gstep": out["steps"][-1]}
    return out


# --------------------------------------------------------------------------- plots
def make_figures(results: dict, outdir: str) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = list(results)
    files = []

    fig, ax = plt.subplots(2, 3, figsize=(15, 8))
    for lab in labels:
        od = results[lab].get("oneddiag", {})
        fd = results[lab].get("fields", {})
        if od.get("species"):
            c = od["species"].get("C", {})
            ukey = f"upar_{c.get('best', 'total')}_traces_m_s"
            if ukey in c:
                for p, tr in c[ukey].items():
                    ax[0, 0].plot(od["gstep"], np.array(tr) / 1e3, label=f"{lab} psi {p}")
            prof = c.get(f"density_{c.get('best', 'total')}_rel_noise_profile")
            if prof:
                ax[0, 1].plot(od["psi"], prof, label=lab)
        if "pot00" in od:
            for p, tr in od["pot00"]["traces_V"].items():
                ax[0, 2].plot(od["gstep"], tr, label=f"{lab} psi {p}")
        w = fd.get("weights", {})
        for sp in ("e", "D", "C"):
            key = f"{sp}_weight_variance_core_mean"
            if key in w:
                ax[1, 0].semilogy(fd["steps"], np.maximum(w[key], 1e-30), label=f"{lab} {sp}")
        r = fd.get("f2d", {}).get("nC_over_nC0", {})
        if r:
            for band in ("0.2-0.6", "0.9-1.0"):
                if f"roughness_{band}" in r:
                    ax[1, 1].plot(fd["f2d"]["steps"], r[f"roughness_{band}"], label=f"{lab} {band}")
        r = fd.get("pot0", {}).get("roughness_over_Te", {})
        if r:
            for band in ("0.2-0.6", "0.9-1.0"):
                if band in r:
                    ax[1, 2].plot(fd["steps"], r[band], label=f"{lab} {band}")
    ax[0, 0].set(title="carbon u_par (f0+df) at psi probes", xlabel="gstep", ylabel="km/s")
    ax[0, 1].set(title="carbon FSA density: relative HF noise", xlabel="psi_n", ylabel="rms resid / mean", yscale="log")
    ax[0, 2].set(title="pot00_1d at psi probes", xlabel="gstep", ylabel="V")
    ax[1, 0].set(title="particle weight variance (core mean)", xlabel="gstep")
    ax[1, 1].set(title="n_C grid roughness / n_C0 (f2d)", xlabel="gstep", yscale="log")
    ax[1, 2].set(title="pot0 grid roughness / Te (2d)", xlabel="gstep", yscale="log")
    for a in ax.ravel():
        a.legend(fontsize=7); a.grid(alpha=0.3)
    fig.tight_layout()
    p = os.path.join(outdir, "noise_traces.png"); fig.savefig(p, dpi=120); plt.close(fig); files.append(p)

    fig, ax = plt.subplots(1, 3, figsize=(15, 4.5))
    for lab in labels:
        od = results[lab].get("oneddiag", {})
        fd = results[lab].get("fields", {})
        c = od.get("species", {}).get("C", {})
        best = c.get("best", "total")
        if f"density_{best}_final_profile" in c:
            ax[0].plot(od["psi"], np.array(c[f"density_{best}_final_profile"]) / 1e18, label=f"{lab} final ({best})")
        if f"density_{best}_initial_profile" in c:
            ax[0].plot(od["psi"], np.array(c[f"density_{best}_initial_profile"]) / 1e18, "--", lw=0.8, label=f"{lab} initial")
        mp = fd.get("f2d", {}).get("midplane_last")
        if mp:
            ax[1].plot(mp["psin"], mp["nC_over_nC0"], ".", ms=3, label=f"{lab} g{mp['gstep']}")
        mp = fd.get("pot0", {}).get("midplane_last")
        if mp:
            ax[2].plot(mp["psin"], mp["pot0_V"], ".", ms=3, label=f"{lab} g{mp['gstep']}")
    ax[0].set(title="carbon FSA density (f0+df)", xlabel="psi_n", ylabel="1e18 m^-3")
    ax[1].set(title="outboard midplane n_C / n_C0 (f2d, last)", xlabel="psi_n")
    ax[2].set(title="outboard midplane pot0 (last)", xlabel="psi_n", ylabel="V")
    for a in ax:
        a.legend(fontsize=7); a.grid(alpha=0.3)
    fig.tight_layout()
    p = os.path.join(outdir, "profiles_last.png"); fig.savefig(p, dpi=120); plt.close(fig); files.append(p)
    return files


# ---------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="LABEL=RUN_DIR")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-fig", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    runs = [item.split("=", 1) for item in args.runs]
    meshes: dict = {}
    for label, run_dir in runs:
        try:
            meshes[label] = load_mesh(run_dir)
        except Exception as exc:  # noqa: BLE001
            meshes[label] = exc

    # A run whose f0 file lacks a species (e.g. a full-f species has no f0 grid) borrows the
    # initial profiles from a run on the same mesh that has all three species.
    reference = next((lab for lab, m in meshes.items() if isinstance(m, dict) and m.get("f0_den") is not None
                      and m["f0_den"].shape[0] >= len(SPECIES)), None)

    results: dict = {}
    for label, run_dir in runs:
        print(f"== {label}: {run_dir}", flush=True)
        rec: dict = {"run_dir": run_dir}
        rec["log"] = parse_log(run_dir)
        mesh = meshes[label]
        if isinstance(mesh, Exception):
            rec["mesh_error"] = str(mesh); mesh = None
        else:
            rec["mesh"] = {"nnode": mesh["nnode"], "psix": mesh["psix"]}
            nsp_f0 = 0 if mesh.get("f0_den") is None else mesh["f0_den"].shape[0]
            rec["mesh"]["f0_species_in_file"] = nsp_f0
            if nsp_f0 < len(SPECIES) and reference and reference != label \
                    and meshes[reference]["nnode"] == mesh["nnode"]:
                mesh["f0_den"] = meshes[reference]["f0_den"]
                mesh["f0_T_ev"] = meshes[reference]["f0_T_ev"]
                rec["mesh"]["f0_profiles_from"] = reference
        try:
            rec["oneddiag"] = analyse_oneddiag(run_dir, rec.get("mesh", {}).get("psix"))
        except Exception as exc:  # noqa: BLE001
            rec["oneddiag_error"] = str(exc)
        if mesh is not None:
            try:
                rec["fields"] = analyse_fields(run_dir, mesh)
            except Exception as exc:  # noqa: BLE001
                rec["fields_error"] = str(exc)
        results[label] = rec

    with open(os.path.join(args.out, "carbon_marker_noise.json"), "w") as fh:
        json.dump(results, fh, indent=1)
    if not args.no_fig:
        for p in make_figures(results, args.out):
            print("figure:", p)

    # Compact console summary
    for lab, rec in results.items():
        lg = rec.get("log", {})
        od = rec.get("oneddiag", {})
        fd = rec.get("fields", {})
        c = od.get("species", {}).get("C", {})
        print(f"{lab}: steps {od.get('gstep_last')} finished={lg.get('finished')} mixed_warn={lg.get('mixed_marker_warning')} "
              f"s/step={lg.get('main_loop_s_per_step')}")
        print(f"   C density rel noise (0.3-0.95): total {c.get('density_total_rel_noise_mean_0.3-0.95')}, "
              f"particle {c.get('density_particle_rel_noise_mean_0.3-0.95')}")
        r = fd.get("f2d", {}).get("nC_over_nC0", {})
        if r:
            print("   n_C roughness last:", {k: v[-1] for k, v in r.items() if k.startswith("roughness")})
        r = fd.get("pot0", {}).get("roughness_over_Te", {})
        if r:
            print("   pot0 roughness/Te last:", {k: v[-1] for k, v in r.items()})
        w = fd.get("weights", {})
        print("   weight variance last:", {k: v[-1] for k, v in w.items() if k.endswith("weight_variance_core_mean")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
