# Rotation-input matching (`flow_match`)

XGC takes the equilibrium toroidal rotation as an input profile
`eq_flow_file` (`omega(psi_n)` in rad/s; with the default `eq_flow_type=2`
the code uses `u_par = omega R B_phi/B`). The neoclassical state an XGCa run
settles into does not reproduce that input at the midplane: the measured
quantity is the carbon toroidal velocity from CER, and the input has to be
corrected until the saturated simulation matches it. On DIII-D 193793 this
was done by hand three times (n600pe, n604pe, n606pe, each taking the
previous run's midplane carbon flow); this package makes that loop
repeatable and catalogued.

```
                 +--------------------------------------------------+
   omega_k.prf --> stage (reserve id, write input, push prf) -> submit (XGCa)
                 |                                                  |
                 |   analyze: f3d carbon u_para -> outboard midplane |
                 |            -> omega_sim(psi_n), saturation check   |
                 |                                                  v
   omega_k+1 <---+-- adjust: omega_k + relax * smooth(omega_target - omega_sim)
```

Like `f_init/`, this is deliberately outside `tools/`: it is loaded only when
a matching study is running. Humans use `xgc-flowmatch`; every write or
submit is plan-only without `--confirm`.

## Files

| Path | Content |
|---|---|
| `cases/<name>.yaml` | one study: machine, template, target, species, smoothing, per-machine job settings |
| `cases/<name>/input` | XGC input template; `eq_flow_file`, `sml_mstep`, `sml_input_file_dir` are overridden per iteration |
| `cases/<name>/adios2cfg.xml`, `petsc.rc` | copied into every run directory |
| `cases/<name>/<target>.prf`, `<initial>.prf` | measured toroidal velocity (or omega) and the starting input |
| `cases/<name>/iterations/<label>/` | per iteration: staged `input`, `midplane_<label>.{json,prf,png}`, `adjust_<label>.{json,png}`, the next `.prf` |
| `cases/<name>.log.yaml` | one entry per action (stage, submit, status, analyze, adjust) |
| `remote/midplane_flow.py` | login-node helper (adios2 + numpy + matplotlib only) |

## One iteration

The two live studies are `nt193793_polaris` and `nt193793_stellarai` (started
2026-09-29 from the original CER omega); both share the case directory
`cases/nt_torque_193793/` (`case_dir:`) but keep their own log, `iterations/`,
archive and profile prefix, so the loops never touch each other's files.

```bash
xgc-flowmatch nt193793_polaris plan   --prf flow_match/cases/nt_torque_193793/omg_d193793_1700ms_SOL.prf
xgc-flowmatch nt193793_polaris stage  --prf ... --label it01 --confirm   # reserves n???po, writes input, pushes the prf
xgc-flowmatch nt193793_polaris submit it01                               # shows nodes / wall time / dryrun
xgc-flowmatch nt193793_polaris submit it01 --confirm
xgc-flowmatch nt193793_polaris status --label it01
xgc-flowmatch nt193793_polaris next it01                                 # when completed: analyze + archive + adjust + plan it02
xgc-flowmatch nt193793_polaris stage --prf flow_match/cases/nt193793_polaris/iterations/it01/omg_..._it01.prf --confirm
```

## Unattended nights

`python -m flow_match.unattended <case> --until "YYYY-MM-DD HH:MM" [--poll 600] [--max-submits 4]`
polls the newest iteration and, when it completes, runs analyze -> adjust ->
plan -> stage -> submit of the next one with the case's own job settings. It
stops at the deadline, after `--max-submits`, or on any failed/cancelled run,
and logs to `cases/<case>.unattended.log`. Start it detached on the
controller (`setsid nohup ... &`); it is the user's standing approval for
that window, so run it only when they have given one. With the `automation`
backend on flux the numbers are minted in flux's copy of `kb/`, which has to
be copied back to the Mac afterwards.

`--machine stellarai --kind dev --mstep 20` on `plan`/`stage` runs the same
case as a short dev test on another machine (the mesh, equilibrium and
profile files must already be in that machine's `remote_input_dir`).

## What `analyze` computes

1. Reads `xgc.f2d.<step>.bp` (XGCa; `xgc.f3d.<step>.bp` for XGC1) for the last `analysis.last` steps and averages
   `species.f3d_var` (carbon: `i2u_para`; the third species has no underscore
   in its prefix, unlike `e_` and `i_`).
2. Interpolates the mesh field along `Z = Z_axis`, `R > R_axis` with a
   linear triangular interpolator; the same line gives `psi_n`, `R` and
   `B_phi/B` (`xgc.mesh.bp`, `xgc.equil.bp`, `xgc.bfield.bp`).
3. `omega_sim = u_para / (R B_phi/B)`, `v_tor = u_para B_phi/B`.
4. Saturation: `omega` at the `analysis.probes` surfaces for every step in
   the window; the relative change between the two halves of the window is
   compared with `analysis.saturation_tol`. An unsaturated run is reported,
   not refused: extend the run (restart) or accept the average knowingly.

## What `adjust` computes

The comparison is made in **toroidal velocity** at the outboard midplane,
because that is what CER measures:

- `v_sim = u_para * B_phi/B` (the carbon parallel flow projected toroidally,
  from the analysis), `v_target` from `target.file` (`target.scale` to m/s).
- `delta_v = v_target - v_sim` on the input file's psi grid.
- The input `omega` implies `v = omega * R * (B_phi/B)^2` (XGC `eq_flow_type=2`:
  `u_par = omega R B_phi/B`), so the correction is
  `omega_next = omega + relax * smooth(delta_v) / (R (B_phi/B)^2)`, then
  smoothed once more (`smooth_delta`, `smooth_new`: Savitzky-Golay, as in the
  notebook `adjust_flow_5th_from_turb_sim.ipynb`).
- Beyond `adjust.sol.psi_start` (the separatrix, where the measurement ends)
  the default `sol.mode: follow` extrapolates the target gently, with the
  slope fitted over the last `fit_width` of the data and a saturating
  exponential of e-folding length `width`
  (`v = v_end + slope * width * (1 - exp(-(psi - psi_end)/width))`), and keeps
  correcting toward it; `taper > 0` fades the SOL correction with a Gaussian
  of that width. `hold` carries the separatrix correction, `constant` and
  `zero` impose a value. Inside `adjust.psi_min` the inner value is held.
- Metrics: rms and max of `delta_v` (m/s) in the trusted range, relative to
  the target rms, plus the rms of the omega correction. Stop when the
  relative rms stops decreasing between iterations. The update is a
  fixed-point iteration (unit response assumed); a secant update from two
  iterations' (input, output) pairs can be added later since both live in
  the archive.

## The study archive (`.aca`)

With `campaign.enabled: true` every `analyze` appends the iteration to one
HPC-Campaign archive per case, built on the compute machine's login node
(`hpc_campaign manager` from that machine's campaign venv, host alias from
`machines.local.yaml`) and pulled to `campaign.local_store` on the
controller:

| entry | kind | how it is read on the controller |
|---|---|---|
| `<label>/input`, `<label>/omega_input.prf` | text, embedded (`--store`) | SQLite + zlib, no connector |
| `<label>/midplane.json`, `<label>/midplane.prf` | text, embedded | same; **`adjust` reads this** |
| `<label>/xgc.oneddiag.bp`, mesh, equil, bfield | ADIOS, remote by host alias | campaign connector |
| `<label>/xgc.f2d.<step>.bp` of the averaging window | ADIOS, remote | campaign connector |

So the loop state is in the archive: `adjust` on any host with the `.aca`
continues from the same analysis. `xgc-flowmatch <case> campaign --list`
shows the iterations, `--update <label>` rebuilds one, `--verify <label>`
reads `xgc.oneddiag.bp/time` through the connector (`hpc_campaign connector
-c ~/.config/hpc-campaign/hosts.yaml -p 30000` must be running on that host).
Verified 2026-09-28 from flux on the stellar-ai run d0023sa.

## Backends: who records the run

`backend:` in the case yaml (or `--backend` on `plan`/`stage`/`next`):

| backend | run directory | submission | records |
|---|---|---|---|
| `automation` (default) | canonical `<run_root>/<simulation_id>_<short_name>` from a reserved global id | `hpc_runner.submit` (dryrun gate, provenance, input backup, CSV notebook, `ai_note`) | catalog + case log |
| `direct` | `<run_root>/<case>_<label>` | same job-script template, `sbatch` over SSH | case log (+ campaign archive) only |

`direct` is for a controller that has no catalog, such as flux: it needs
only this package with `tools/ssh.py`, `tools/scheduler.py`,
`tools/machines.py` and `tools/hpc_runner.py` (for the script template).
It mints no simulation number; a run that is later wanted in the catalog is
adopted with `xgc-sim adopt-remote`. Agreed split (2026-09-28): dev tests and
the first iterations run from the Mac with `automation`; long unattended
loops move to flux with `direct`.

## Catalog

Every staged iteration reserves its own `simulation_id` (production by
default, `--kind dev` for tests) with tags `flow-match`, the case name and
the label, and is submitted through `hpc_runner.submit`, so provenance,
input backup, the CSV notebook and `ai_note` are handled as for any run.
