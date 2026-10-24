"""Audit n695pe/n696pe separatrix transport using existing bounded reductions.

No remote execution or writes unless --output is supplied. Run from the checkout:
  .venv/bin/python -m analysis.tcv_separatrix_diagnosis [--output DIRECTORY]
Uses physical total-f *_df_1d moments and the Hahm-Burrell OMP proxy based on
Phi00. This is not the full poloidally varying n=0 flow, nor a causal shear test.
Rates have units s^-1 (no division by 2*pi). Gradients use dpsi/dr=R*Bpol;
maximum-R mesh nodes are not necessarily at the same Z and cannot define dr.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from tools import paths

RUNS = {'n695pe': 'run-20261009T014252Z', 'n696pe': 'run-20261010T023959Z'}
WINDOW = (1500, 1990)  # identical windows; PT last output is 1990


def smooth(y, w):
    x = np.arange(-3 * w, 3 * w + 1)
    k = np.exp(-.5 * (x / w) ** 2)
    return np.convolve(np.pad(y, 3 * w, mode='edge'), k / k.sum(), mode='valid')


def temperature(b, s):
    return 2 / 3 * (b[f'od_{s}_perp_temperature_df_1d'] + b[f'od_{s}_parallel_mean_en_df_1d'])


def analyze(sid, rid):
    state = paths.RUNS_DIR / 'master' / rid / 'summaries'
    basic = paths.product_target(state / f'{sid}_basic_2000.npz')
    modes = paths.product_target(state / f'{sid}_modestruct_1500-2000.npz')
    with np.load(basic) as f:
        b = dict(f)
    with np.load(modes) as f:
        m = dict(f)
    g = b['od_gstep']
    keep = np.array(sorted({int(v): j for j, v in enumerate(g)}.values()))
    for key, v in b.items():
        if key.startswith('od_') and v.ndim and len(v) == len(g) and key not in ('od_psi', 'od_psi00'):
            b[key] = v[keep]
    g, p, p0, px = b['od_gstep'], b['od_psi'], b['od_psi00'], float(b['psix'])
    w = (g >= WINDOW[0]) & (g <= WINDOW[1])
    om, nd = m['omp_surfs'], m['omp_nodes']
    po, R = m['psi_surf'][om], m['rz'][nd, 0]
    rbp = R * m['bpol'][nd]
    B = m['bmag'][nd]
    def fields(phi, width):
        d1 = np.gradient(smooth(phi, width), p0 * px)
        er = -np.interp(p0, po, rbp) * d1
        shear = np.interp(p0, po, rbp) ** 2 / np.interp(p0, po, B) * abs(np.gradient(smooth(d1, width), p0 * px))
        return er, shear
    er, shear = fields(b['od_pot00_1d'][w].mean(0), 3)
    edge = (p0 >= .95) & (p0 < 1.)
    record = {'run_id': rid, 'window': list(WINDOW), 'n_oned_samples': int(w.sum()),
              'actual_last_step': int(g[-1]), 'actual_last_time_ms': float(b['od_time'][-1] * 1e3),
              'rate_units': 's^-1, not cycles/s',
              'sources': [{'path': str(f), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()} for f in (basic, modes)],
              'shear_smoothing_sensitivity': {}, 'time_windows': {}, 'radial_metrics': {}}
    for width in (1, 3, 6):
        e, s = fields(b['od_pot00_1d'][w].mean(0), width)
        record['shear_smoothing_sensitivity'][str(width)] = {
            'sigma_psiN': float(width * np.median(np.diff(p0))),
            'Er_min_kV_m': float(e[edge].min() / 1e3),
            'max_shear_1e6_per_s': float(s[edge].max() / 1e6),
            'shear_at_097_1e6_per_s': float(np.interp(.97, p0, s) / 1e6)}
    for lo, hi in ((100, 400), (500, 900), WINDOW):
        sel = (g >= lo) & (g <= hi)
        e, s = fields(b['od_pot00_1d'][sel].mean(0), 3)
        record['time_windows'][f'{lo}-{hi}'] = {'Er_min_kV_m': float(e[edge].min() / 1e3),
            'shear097_1e6_per_s': float(np.interp(.97, p0, s) / 1e6)}
    Te, Ti = temperature(b, 'e'), temperature(b, 'i')
    # ln gradients from diagnostic psi, not from ragged maximum-R coordinates.
    gradTe = -np.gradient(np.log(Te), p * px, axis=1) * np.interp(p, po, rbp)
    gradTi = -np.gradient(np.log(Ti), p * px, axis=1) * np.interp(p, po, rbp)
    ps = m['psi_surf']; sl = m['surf_len']; si = m['surf_idx']
    valid = [s for s in range(len(sl)) if sl[s] >= 8 and .8 <= ps[s] <= 1.000001 and np.mean(m['region'][si[s, :sl[s]]] == 3) < .5]
    phir, dnr, C = [], [], []
    for s in valid:
        idx = si[s, :sl[s]]; vol = m['node_vol'][idx]
        avg = lambda a: np.average(a[idx], weights=vol)
        phir.append(np.sqrt(avg(m['phi2'])) / np.interp(ps[s], p, Te[w].mean(0)) * 100)
        dnr.append(np.sqrt(avg(m['ne2'])) / np.interp(ps[s], p, b['od_e_gc_density_df_1d'][w].mean(0)) * 100)
        C.append(-avg(m['ne_eth']) / np.sqrt(avg(m['ne2']) * avg(m['eth2'])))
    dp = np.diff(po) * px / (.5 * (rbp[1:] + rbp[:-1]))
    cc = m['corr_xx'] / np.sqrt(np.outer(np.diag(m['corr_xx']), np.diag(m['corr_xx'])))
    Qe = b['od_e_gc_density_df_1d'] * b['od_e_radial_en_flux_ExB_df_1d'] / np.sqrt(b['od_e_grad_psi_sqr_1d'])
    for target in (.95, .97, .99):
        j = int(np.argmin(abs(po - target)))
        record['radial_metrics'][str(target)] = {
            'shear_1e6_per_s': float(np.interp(target, p0, shear) / 1e6),
            'dn_rms_percent': float(np.interp(target, ps[valid], dnr)),
            'phi_rms_percent': float(np.interp(target, ps[valid], phir)),
            'particle_flux_proxy_correlation': float(np.interp(target, ps[valid], C)),
            'inverse_LTe_initial_per_m': float(np.interp(target, p, gradTe[0])),
            'inverse_LTe_mean_per_m': float(np.interp(target, p, gradTe[w].mean(0))),
            'inverse_LTi_initial_per_m': float(np.interp(target, p, gradTi[0])),
            'inverse_LTi_mean_per_m': float(np.interp(target, p, gradTi[w].mean(0))),
            'electron_total_ExB_energy_flux_kW_m2': float(np.interp(target, p, Qe[w].mean(0)) / 1e3),
            'nearest_omp_surface_psiN': float(po[j]),
            'omp_spacing_inward_mm': float(dp[j-1] * 1e3),
            'omp_correlation_inward': float(cc[j,j-1])}
    return record, dict(b=b, m=m, p=p, p0=p0, er=er, shear=shear, ps=ps[valid], dn=np.array(dnr), phi=np.array(phir), C=np.array(C),
                        gradTe=gradTe[w].mean(0), gradTi=gradTi[w].mean(0), Qe=Qe[w].mean(0))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--output', type=Path); args = ap.parse_args()
    results, curves = {}, {}
    for sid, rid in RUNS.items():
        results[sid], curves[sid] = analyze(sid, rid)
    print(json.dumps(results, indent=2, allow_nan=False))
    if args.output:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        args.output.mkdir(parents=True, exist_ok=True)
        target = args.output / 'tcv_separatrix_diagnosis.json'
        if target.exists():
            raise FileExistsError(target)
        target.write_text(json.dumps(results, indent=2, allow_nan=False) + '\n')
        fig, axs = plt.subplots(2, 2, figsize=(12, 8), layout='constrained')
        for sid, x in curves.items():
            c = '#bf4a32' if sid == 'n695pe' else '#2766a0'
            label = ('NT ' if sid == 'n695pe' else 'PT ') + sid
            sel0 = (x['p0'] >= .9) & (x['p0'] < 1.)
            sel1 = (x['p'] >= .9) & (x['p'] <= 1.)
            sels = (x['ps'] >= .9) & (x['ps'] <= 1.000001)
            axs[0,0].plot(x['p0'][sel0], x['er'][sel0]/1e3, c=c, label=label)
            axs[0,1].plot(x['p0'][sel0], x['shear'][sel0]/1e6, c=c, label=label)
            axs[1,0].plot(x['ps'][sels], x['dn'][sels], c=c, label=label)
            axs[1,1].plot(x['p'][sel1], x['Qe'][sel1]/1e3, c=c, label=label)
        titles = ['Mean radial electric field [kV/m]', 'Mean-field shearing-rate proxy [$10^6$ s$^{-1}$]',
                  r'Electron fluctuation $\delta n_{rms}/n$ [%]', 'Electron ExB energy flux (all n) [kW/m$^2$]']
        for ax, title in zip(axs.flat, titles):
            ax.set_title(title); ax.set_xlim(.9, 1.001); ax.axvline(1., color='.5', ls=':'); ax.set_xlabel(r'$\psi_N$'); ax.grid(alpha=.2); ax.legend()
        axs[0,1].set_ylim(0,None); axs[1,0].set_ylim(0,None); axs[1,1].set_ylim(0,None)
        fig.suptitle('TCV collisionless ES: strong edge shear, finite fluctuations, falling transport\n'
                     'Mean steps 1500-1990; fluctuation reduction 1500-2000 (NT) / 1500-1990 (PT)', fontsize=12)
        image_target = paths.product_target(args.output / 'tcv_separatrix_diagnosis.png')
        if image_target.exists():
            raise FileExistsError(image_target)
        image_target.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(image_target, dpi=160)
        plt.close(fig)


if __name__ == '__main__':
    main()
