# Submission workflow

The path from an input file to a running job, and the checks in between.

```
  plan_job          render script, estimate node-hours       local, free
       |
  validate_input    xgc --dryrun on the login node           ~3 s, free
       |
  [human approves]  review script + cost + dryrun report
       |
  submit_job        re-runs the dryrun, then sbatch          consumes allocation
       |
  job_status        pending -> running -> completed
       |
  job_output        tail of stdout/stderr
```

## Why validate before submitting

`xgc --dryrun` runs the real XGC binary, so it rejects exactly what a real run
would reject. It parses the namelist, loads the mesh, predicts memory, and
checks particle resolution, then returns before PETSc/ADIOS2 initialization.

It costs about 3 seconds on one login-node rank and consumes no allocation. A
bad namelist caught here saves a queue wait plus the node-hours a real run would
have burned before dying.

Exit codes: `0` accepted (warnings may remain), `1` rejected.

## The two gates in submit_job

Both live in `tools/hpc_runner.py`, not in the MCP layer, so they hold for any
caller:

1. **Confirmation** — without `confirm=True`, `submit_job` returns the plan and
   touches nothing remote (AGENTS.md, absolute rule 1).
2. **Dryrun** — when `case_dir` is given, a rejected input blocks submission
   even with `confirm=True`. `skip_dryrun=True` overrides this and should only
   be used when the user explicitly asks.

Always pass `case_dir` for real XGC runs. It is unnecessary for `hello_world`
jobs, which do not read an input file.

## Reading the dryrun report

| Field | Meaning |
|---|---|
| `removed` | **Blocking.** Parameters deleted from XGC. The job cannot run. |
| `deprecated` | Still accepted, but renamed or restructured. Worth fixing. |
| `warnings` | Free-form, usually particle resolution. |
| `memory.per_rank_gb` | Predicted host memory per rank. |
| `memory.available_per_rank_gb` | What the node actually has. |
| `resolution.particles_per_vertex` | XGC wants ~5 for TotalF markers, ~0.05 for ReducedDeltaF. |
| `parallelism` | Ranks, threads, and field model the dryrun assumed. |

Pass `n_ranks` matching the real job: memory and resolution estimates scale with
it. `submit_job` does this automatically from the plan.

## Reference case: ECBC

`/home/seungku/tmp/test_ecbc` — ECBC electrostatic delta-f, core simulation,
n=6. Recorded in `kb/cases/ecbc.yaml`.

**Status: passes cleanly** (0 removed, 0 deprecated, 0 warnings) as of
2026-07-28 against XGC-Devel `256c3098`. At 64 ranks: 0.146 GB per rank against
375.9 GB available, 0.0517 particles per 5D vertex.

It did not start out that way. The original predated current XGC and was
rejected outright; it was migrated on 2026-07-28, with the original preserved at
`test_ecbc.bak.20260728`.

Removed parameters (these blocked startup):

| Removed | Replacement |
|---|---|
| `ptb_3db_on` | `sml_3db_on` — note it also moves to `&sml_param` |
| `sml_poisson_solver_type` | dropped; `sml_iter_solver*` already configures the solver |
| `sml_f0_grid_alpha` | dropped (was 0.0) |
| `sml_f0_grid_alpha_start` | dropped, no longer used |
| `f0_update_analytic_alpha` | renamed to `f0_update_alpha` |

Deprecated parameters, migrated at the same time: `sml_deltaf`,
`sml_deltaf_elec`, `sml_f0_grid`, `sml_special`, `sml_em_pullback_mode`,
`smooth_rad_efield`.

### The marker type mattered more than it looked

`sml_deltaf` and `sml_deltaf_elec` are **inert** in current XGC — they appear
only in the deprecation table and nothing reads them. The case was therefore
silently running as `TotalF`, not delta-f, and the dryrun's low-resolution
warning was the visible symptom: 0.0517 ptl/vertex is on target for
`ReducedDeltaF` but roughly 100x short for `TotalF`.

Declaring `ptl_marker_type='ReducedDeltaF'` for both species restores the
intended behavior and clears the warning. It also brings `weight_evo_eq=PDE`
and `dynamic_f0=false` by default, which is consistent with the case having had
`sml_f0_grid=.false.`.

This is worth generalizing: a deprecation warning can mean the run is quietly
doing something other than what the input file appears to say.
