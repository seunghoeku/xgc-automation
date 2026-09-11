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
| 2 | Case 1 with a dedicated XGCa mesh in the middle; the f_init is interpolated onto the turbulence mesh (`convert_distribution`). | not yet |

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

**B.** `make-finit` runs `remote/make_f_init.py` on the login node. It reads
the last f0 snapshot with `XGCDistribution.from_xgc_output(...,
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
from `dt0_<tag>/xgc.2d.<cmp>.bp` (default: the first output), forms
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

The flux-surface average is `xgc_reader.distribution.core.flux_surface_average`
(per-surface average scattered back through `mesh.psi_surf`). The notebook
interpolated through the 1-D diagnostic psi grid instead; on cbc6 both grids
have 68 points and coincide, and the library route does not depend on that.

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
`xgc.particle.*.bp`. The f_init runs are shifted by the time of the f0 step
they came from so they continue the precursor curve. JSON and PNGs are pulled
to `f_init/cases/<case>/transient_psi<psi>*`; a summary table (transient
peaks, end-of-run shear scatter, last noise level) is printed and logged.

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
xgc-finit cbc6 compare                                  # paper-style transient figures
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
