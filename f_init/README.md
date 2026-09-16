# Non-Maxwellian `f_init` procedure

An XGC turbulence run started from a Maxwellian spends its first fraction of a
millisecond relaxing to a neoclassical state, and the transient pollutes the
early heat flux. The `f_init` procedure removes that transient: run XGCa (the
axisymmetric, neoclassical code) first, take its relaxed distribution as the
initial `f_g` of the real run, and correct the small charge-density mismatch
that the transfer introduces.

This directory is deliberately separate from `tools/`. It is used only when
a new non-Maxwellian start is being prepared, so nothing here is loaded into
the everyday context: an agent reaches it through the `f-init` skill, a human
through the `xgc-finit` CLI. The reference is the notebook
`cbc3_f_init_precedure_newer.ipynb` (shot_data/0.ipynb/f_init_paper2); this
code reproduces its "make distribution" and "check dt=0" cells for one
smoothing setting.

## The three cases

| Case | Meaning | Status |
|---|---|---|
| 0 | XGCa on the XGCa mesh, f_init, dt=0 check, density adjustment. The adjusted file is consumed by an XGCa run on the same mesh. | **implemented** |
| 1 | Build the XGCa input from a turbulence input (same mesh), then case 0, then use the adjusted f_init in the turbulence run. | not yet |
| 2 | Case 1 with a dedicated XGCa mesh in the middle; the f_init is interpolated onto the turbulence mesh (`convert_distribution`). | interpolation step (`convert`) implemented; the turbulence input is still prepared by hand |
| 3 | No XGCa run at all: an existing **turbulence** run's own f0 is the relaxed distribution, already on the target mesh. | `average_f0` + `make-finit` implemented (d3dqh); the dt=0 check reads `xgc.3d` |

## Case 3: starting from a turbulence run's own f0

Sometimes the relaxed distribution to start from is not an XGCa run but a
turbulence run that has already been going for thousands of steps -- the
f_init files it was itself started from being long gone (d3dqh: they lived in
another centre's scratch). Two things separate that f0 from what stage B
expects, and `remote/average_f0.py` removes both:

* the XGC1 array carries a toroidal dimension, `(nphi, nmu+1, nnode, nvp)`;
* one snapshot of a production mesh is ~100 GB, so neither the toroidal
  average nor a time average fits in a login node's memory in one piece.

It streams each snapshot one toroidal plane at a time, accumulates the sum
over planes and over the requested snapshots, and writes `xgc.f0.<step>.bp`
with `nphi=1` -- the shape XGCa writes, so `from_xgc_output` reads it
unchanged. Peak memory is one plane of one species (about 1 GB on a 137k-node
mesh). Averaging over a handful of snapshots is the point: a single turbulent
snapshot carries the instantaneous fluctuation, and the f_init is meant to be
the relaxed background.

```bash
python3 f_init/remote/average_f0.py \
    --run-dir <turbulence run> --out-dir <case root>/maxwell \
    --steps 7500 8000 8500 9000 --out-step 9000
```

`maxwell/` then holds that one file plus symlinks to the turbulence run's
`xgc.f0.mesh.bp`, `xgc.mesh.bp`, `xgc.units.bp`, `units.m` and
`xgc.bfield.bp`. There is no stage A job to submit, `status`/`extend-maxwell`
have nothing to report, and because the mesh is already the turbulence mesh
the case-2 `convert` step is not needed either. Start at `make-finit`.

Keep `sml_input_file_dir` short in the stage A template: XGC truncates it at
65 characters and `xgc --dryrun` accepts the over-long value anyway, so the
dt=0 job dies a minute in with a `Cannot open file` on a path cut mid-directory
(d3dqh, 2026-09-15). A relative `'../XGC_inputs/'` beside a symlink in the case
root is the safe form.

Two consequences for the later stages:

* **XGC1 writes `xgc.3d` and no `xgc.2d`.** Both carry `iden` on the mesh
  nodes -- 3d with a leading toroidal dimension that is averaged away -- so
  `adjust`/`check` resolve the density output per directory and fall back to
  `xgc.3d`. The payload records which was used (`ref_source`, `cmp_source`)
  and the CLI prints it.
* **The dt=0 run is an XGC1 run**, not XGCa, so `job.script` must point at a
  turbulence job script.

### More than one species

A case that initialises several kinetic species lists them under
`species_set`, each entry overlaying the shared `species` block, and
`make-finit --species <name>` selects one:

```yaml
species:            # shared
  has_electron: true
species_set:
  e: {var_string: e_f,  mass_au: 5.454e-4, charge_num: -1.0, slot: 1}
  D: {var_string: i_f,  mass_au: 2.0,      charge_num:  1.0, slot: 2}
  C: {var_string: i2f,  mass_au: 12.0,     charge_num:  6.0, slot: 3}
```

The name is prepended to the file tag, so the three runs share one stage A
directory without colliding and the species stays visible in every derived
name: `xgc.f_init_D_smooth_0.5_0.5.bp` ->
`xgc.f_init_adjusted_D_smooth_0.5_0.5.bp`, `dt0_D_smooth_0.5_0.5/`. A case
without `species_set` keeps the untagged names it always had.

## Case 0, stage by stage

```
A  XGCa run from a Maxwellian        maxwell/            sbatch xgcs.cpu   (8 nodes, ~25 min for cbc6)
B  f0 snapshot -> f_init files       maxwell/xgc.f_init_*.bp      login node, minutes
C  dt=0 check run                    dt0_<tag>/          sbatch xgcs.cpu   (8 nodes, ~1 min)
D  density comparison + adjustment   maxwell/xgc.f_init_adjusted_<tag>.bp   login node
E  optional: C + D again with the adjusted file until the mismatch is small
```

**A.** The case root (for example `$PSCRATCH/f_init/cbc6`) holds `maxwell/`
with `input`, `adios2cfg.xml`, `petsc.rc`, `runscript.cpu`, and the sbatch
wrapper `xgcs.cpu`. This is prepared by hand today (the CLI can still submit
it: `submit maxwell`). It must run long enough to reach a neoclassical
steady state and write `xgc.f0.<last>.bp` and `xgc.2d.<last>.bp` at the same
step; cbc6 used `sml_dt=2e-3`, `sml_mstep=2000`, `diag_f0_period=100`.

**A, continued.** `extend-maxwell` resubmits the stage A job unchanged so
XGC resumes from the newest `restart_dir/xgc.restart.<step>.bp` and runs
another `sml_mstep` steps (cbc6: 2000 -> 4000, 0.31 -> 0.62 ms, for the
paper's long Maxwellian trace). Only the sbatch script is rewritten (job name,
stale dependency dropped). Once stage A runs past the snapshot the f_init came
from, set `f0_step` in the case file: it becomes the default snapshot for
`make-finit`, the reference `xgc.2d` step for `adjust`/`check`, and the time
offset for `compare`; otherwise those default to the last f0 step.

**B.** `make-finit` runs `remote/make_f_init.py` on the login node. It reads
the last f0 snapshot (or `f0_step`) with `XGCDistribution.from_xgc_output(...,
use_initial_moments=True)`, removes the analytic Maxwellian, and saves
`xgc.f_init_no_smooth.bp`. If either smoothing width is positive it also
applies `poloidal_smooth_f_init(sigma_theta)` (Gaussian along each flux
surface, radians) and `vspace_smooth(v_width)` (Gaussian in velocity space,
grid cells) and saves `xgc.f_init_smooth_<sigma>_<width>.bp`. Numbers are
rendered the way the notebook did (`0.3_0.5`, `0_0`, `1_0.5`). In a smoothing
scan the no-smooth file already exists after the first call; it is then kept
as is (logged as `reused`) and only the new smooth file is written, so
`--overwrite` is needed only to replace a file that really exists.

**C.** `stage-dt0 --finit <file>` copies the job files from `maxwell/` into
`dt0_<tag>/` and edits the input in place so the diff is reviewable:

| parameter | value | why |
|---|---|---|
| `sml_dt` | `2.0D-13` | effectively zero: particles do not move, so the density XGC reconstructs is the one implied by `f_init` alone |
| `sml_mstep` | `20` | enough for two 2d outputs at `diag_*_period=10` |
| `sml_restart` | `.false.` | fresh start regardless of how stage A was restarted |
| `ptl_input_file_dir` | `'../maxwell/'` | the f_init files live with the run that produced them |
| `ptl_f_init_file` slot *k* | `'<file>'` | the species being initialised (slot 2 = deuterium in cbc6) |
| `ptl_maxwellian_init` slot *k* | `.false.` | read `f_init` instead of the analytic Maxwellian |

The `#SBATCH --job-name` becomes `<case>_dt0_<tag>` and any stale
`--dependency` line is dropped. Nothing is written without `--confirm`, and an
existing directory is not replaced without `--overwrite`.

**Submission.** `submit <dir> --confirm` runs `xgca --dryrun` in the directory
first, with the module lines from the directory's own `runscript.cpu`, and
refuses to submit if the dryrun rejects the input. The node count, wall time,
and node-hour bound are printed before anything is queued (absolute rule 4).
`jobs` reports the state of every job the case has submitted. The queue-watch
timer (`docs/queue-watch.md`) notices these jobs too.

**D.** `adjust <dt0_dir>` runs `remote/adjust_f_init.py`. It reads `iden`
from `maxwell/xgc.2d.<ref>.bp` (default: the f0 step the f_init came from) and
from `dt0_<tag>/xgc.2d.<cmp>.bp` (default: the first output) -- or from
`xgc.3d` in whichever of the two directories has no 2d output, see case 3 --
forms
`dn = n_ref - n_dt0`, flux-surface averages it, and scales the full
distribution node by node:

```
f_adjusted = f_init_total * (1 + factor * <dn>_fsa / n_local)
```

`n_local` is the density moment of `f_init` itself (`update_maxwellian_moments`
with `no_update=True`, so the saved `density` array is unchanged and the
correction lives in `f_g`). The Maxwellian is removed again and the result is
saved next to its source as `xgc.f_init_adjusted_<tag>.bp` (then
`adjusted2_`, ... for further iterations). A JSON summary and a PNG land in
the dt0 directory and are pulled to `f_init/cases/<case>/`.

The flux-surface average is `xgc_reader`'s `fsa_simple` (volume-weighted
per-surface mean) scattered back to the nodes through the surface membership
lists `surf_idx`/`surf_len`; nodes on no stored surface (wall nodes) get no
metric and no scaling, and by default (`adjust_region_max: 1` in the case
file) neither do SOL or private-region nodes: the correction is applied on
closed flux surfaces only, because the SOL of an XGCa run is still relaxing
and its f0-grid density is too noisy to trust as a target (asdexu2,
2026-09-14). Set `adjust_region_max: 2` to include the SOL.
`xgc_reader.distribution.core.flux_surface_average`
is not used because it interpolates surface values onto node psi, and with
the private-flux surfaces appended after the SOL surfaces in `psi_surf` that
interpolation is not monotonic: on asdexu2 (2026-09-14) it handed every node
beyond psi_n 1.16 a private-region `<dn>`, which divided by the tiny far-SOL
density looked like a 50% mismatch. The notebook
interpolated through the 1-D diagnostic psi grid instead; on cbc6 both grids
have 68 points and coincide, and the library route does not depend on that.

**G. Turbulence mesh (case 2).** `convert --finit xgc.f_init_adjusted_<tag>.bp`
runs `remote/convert_f_init.py` on the login node: it reproduces the
`xd.convert_distribution(dist, x_source, x_target)` cell of
`cbc5_f_init_turb_prepare.ipynb`, linearly interpolating every velocity-space
slice of the (Maxwellian-restored) distribution and its moments from the XGCa
mesh triangulation onto the nodes of the turbulence mesh, recomputing the
Maxwellian moments there and removing the Maxwellian again. Target nodes
outside the XGCa mesh take the nearest source node. The target is a directory
holding `xgc.mesh.bp`/`xgc.units.bp` written by XGC on the turbulence mesh
(`turb_mesh_dir` in the case file or `--target-dir`); only its mesh is read.
The result, `xgc.f_init_<tag>_turb.bp` in `maxwell/`, is consumed by a
turbulence input whose `f0_nmu/f0_nvp/f0_smu_max/f0_vp_max` equal the f_init's
velocity grid. A PNG comparing the density and temperature moments on both
meshes lands in `f_init/cases/<case>/`.

**F. Production run.** `stage-run --finit xgc.f_init_adjusted_<tag>.bp`
writes `finit_sm_<sigma>_<width>/` (cbc3's convention; the `adjusted` prefix
is dropped from the directory name, the log records the file). It is stage
A's input with only the f_init slot switched, plus whatever the case file
lists under `run.overrides` (cbc6: the `diag_particle_*` settings, which the
maxwell input already carried). `sml_dt`, `sml_mstep` and `sml_restart` are
kept so a job that hits the wall clock can be restarted like any XGC run.
`submit finit_sm_<tag> --confirm` works as for the dt=0 directories. These
runs are not given registry `simulation_id`s; they live and are logged inside
the f_init case, as the cbc3 runs were.

**G. Transient comparison.** `compare` runs `remote/transient_compare.py`
on the login node over the Maxwellian run and every `finit_*` directory (or
the ones named). It reproduces the paper's CBC figures and the plotting
notebook: radial vorticity flux `-<Gamma_i> eB/(2 m_p n_i0)` and
flux-surface-averaged ExB shear at one `psi_N` (default 0.45), the shear
magnified over the last `--end-window` ms, vorticity flux over `(psi_N, t)`
on one colour scale, the radial energy flux, and the particle-noise rms from
`xgc.particle.*.bp`. Every run starts at t = 0 as in the paper; with
`--shift-finit` the f_init runs are shifted by the time of the f0 step they
came from and continue the precursor curve instead; `--paper` switches to
publication styling (font 28, 13x10 in, thick lines). The file stem gains
`_shift` / `_paper` so the variants coexist. With `--shift-finit` the
vorticity-flux and shear figures carry an inset over the first 0.2 ms after
the f_init start (referee request). `--twod-runs maxwell finit_sm_0.5_0.5`
restricts the `(psi_N, t)` figure to the paper's two panels and
`--twod-scale per-panel` gives each its own labelled colour bar (diverging
map centred at zero) so a quiet run stays readable. The noise measure is the
rms of `w1*w0` over the written marker sample (columns 4 and 7 of `iphase`:
delta-f weight times full-f weight, i.e. physical ions per marker); `rms(w1)`
is kept in the JSON as well. JSON and PNGs are pulled
to `f_init/cases/<case>/transient_psi<psi>*`; a summary table (transient
peaks, end-of-run shear scatter, last noise level) is printed and logged.

**H. Paper Fig. 1 and E_r.** `distribution [--smooth FILE] [--normalize
local|peak] [--paper]` draws `(f - f_M)` of the unsmoothed and the smoothed
f_init at one node (default: outboard midplane nearest `--psi`), either per
cell relative to the local `f_M` (masked where `f_M` is below 1 % of its
peak) or relative to the node's peak `f_M`, with a diverging colour map and a
labelled colour bar. `er-profile [--paper]` draws `<E_r>(psi_N) =
-(d<phi>_00/dpsi) <|grad psi|>` of stage A at the f_init snapshot and at the
end of the run. Both pull JSON and PNGs to `f_init/cases/<case>/`.

**E.** `stage-dt0 --finit xgc.f_init_adjusted_<tag>.bp`, `submit`, then
`check dt0_adjusted_<tag>` prints the residual mismatch without writing a
file. Repeat `adjust` on that directory if another pass is wanted. The
metrics to watch are the rms and max of `<dn>_fsa / n` over closed flux
surfaces (`mesh.region <= 2`); they should drop by roughly the adjust factor.
`n` is always the total density of the f_init file: `iden` in `xgc.2d` is the
*perturbed* gyrocenter density (about 1e17 m^-3 on cbc6 against 4e19 total,
and it crosses zero), so a ratio against `iden` would be meaningless. For
cbc6 the unadjusted no-smooth file gave a node-wise rms of 4e-4 and a
flux-averaged rms of 3e-5.

## Commands

```bash
xgc-finit cbc6 status                                   # what exists in the case root
xgc-finit cbc6 make-finit                               # smoothing from the case file
xgc-finit cbc6 make-finit --sigma-theta 0 --v-width 0   # no-smooth only
xgc-finit cbc6 stage-dt0 --finit xgc.f_init_smooth_0.3_0.5.bp            # plan
xgc-finit cbc6 stage-dt0 --finit xgc.f_init_smooth_0.3_0.5.bp --confirm  # write
xgc-finit cbc6 dryrun dt0_smooth_0.3_0.5
xgc-finit cbc6 submit dt0_smooth_0.3_0.5 --confirm
xgc-finit cbc6 jobs
xgc-finit cbc6 adjust dt0_smooth_0.3_0.5
xgc-finit cbc6 stage-dt0 --finit xgc.f_init_adjusted_smooth_0.3_0.5.bp --confirm
xgc-finit cbc6 submit dt0_adjusted_smooth_0.3_0.5 --confirm
xgc-finit cbc6 check dt0_adjusted_smooth_0.3_0.5
xgc-finit cbc6 stage-run --finit xgc.f_init_adjusted_smooth_0.3_0.5.bp --confirm   # finit_sm_0.3_0.5/
xgc-finit cbc6 submit finit_sm_0.3_0.5 --confirm
xgc-finit cbc6 compare                                  # transient figures, every run from t=0
xgc-finit cbc6 compare --shift-finit --paper            # paper Fig. GAM: f_init runs continue the Maxwellian curve, with insets
xgc-finit cbc6 compare --paper --twod-runs maxwell finit_sm_0.5_0.5 --twod-scale per-panel   # paper Figs. 3 and 4
xgc-finit cbc6 distribution --paper --smooth xgc.f_init_smooth_0.5_0.5.bp --normalize peak   # paper Fig. 1
xgc-finit cbc6 er-profile --paper                       # relaxed E_r(psi_N) of stage A
xgc-finit cbc6 extend-maxwell --confirm                 # continue stage A from its last checkpoint
xgc-finit cbc6 log
```

`--json` on any command gives machine-readable output.

## Files

| Path | Role |
|---|---|
| `cli.py` | `xgc-finit` entry point |
| `procedure.py` | the stages as remote actions over `tools/ssh.py` |
| `config.py` | case files, naming conventions (`dt0_<tag>`, `adjusted_<tag>`), per-case log |
| `namelist_edit.py` | in-place namelist edits including per-species array slots |
| `sbatch.py` | read/rewrite the hand-written `xgcs.cpu`, node-hour estimate |
| `remote/make_f_init.py` | login-node script for stage B |
| `remote/adjust_f_init.py` | login-node script for stages D/E |
| `remote/transient_compare.py` | login-node script for the transient comparison figures (G) |
| `remote/distribution_figure.py` | paper Fig. 1: deviation of f_init from the local Maxwellian at one node (H) |
| `remote/er_profile.py` | relaxed `<E_r>(psi_N)` of stage A (H) |
| `cases/<name>.yaml` | one case: root, species slot, smoothing, dt0 settings, `run.overrides` for production runs |
| `cases/<name>.log.yaml` | what was done, when, with which job ids and metrics |
| `cases/<name>/` | pulled JSON/PNG summaries of the density comparisons |

The login-node interpreter and the XGC_reader checkout come from
`analysis.python` and `analysis.pythonpath` in `machines.local.yaml`; a case
file may override them under `remote:`. The scripts are pushed to
`<root>/.xgc-finit/` before each run, so the copy on the machine is always the
one in this repository.

## Known traps

- `xgc1(path=...)` in XGC_reader does `os.chdir`; the remote scripts use
  absolute paths throughout for that reason.
- `update_maxwellian_moments` adds the Maxwellian back into a `f_g`-only
  distribution as a side effect. The adjust script relies on it (it needs the
  full `f`), then removes it again before saving.
- Stage A with `sml_restart=.true.` and an empty `restart_dir` starts fresh,
  which is how cbc6 was run after two failed attempts; the dt0 copy sets it to
  `.false.` explicitly so a re-staged directory can never pick up a checkpoint.
- The stage A input must reference the mesh and profiles through paths that
  also resolve from the dt0 directory (cbc6 uses `../../../XGC_inputs/`, the
  same depth), because the dt0 directory is a sibling of `maxwell/`.
