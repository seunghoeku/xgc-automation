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

## Known state of the reference case

`/home/seungku/tmp/test_ecbc` (ECBC, electrostatic, delta-f, 8 planes) predates
the current XGC and **is rejected as-is**. Five removed parameters:

| Removed | Replacement |
|---|---|
| `ptb_3db_on` | `sml_3db_on` |
| `sml_poisson_solver_type` | `sml_iter_solver_converge` |
| `sml_f0_grid_alpha` | `f0_update_alpha` |
| `sml_f0_grid_alpha_start` | none, no longer used |
| `f0_update_analytic_alpha` | renamed to `f0_update_alpha` |

Six further parameters are deprecated but still accepted: `sml_f0_grid`,
`sml_deltaf`, `sml_deltaf_elec`, `sml_special`, `sml_em_pullback_mode`,
`smooth_rad_efield`. Most reflect the move to species-specific
`ptl_marker_type`.

A corrected copy at `/tmp/ecbc_fix` passes (`rc=0`). It is scratch, not a
maintained artifact — the fixes above still need to be applied to a real case
directory.

With the corrected input at 64 ranks the dryrun reports 0.146 GB per rank
against 375.9 GB available, and 0.052 particles per 5D vertex, which triggers
the low-resolution warning. That value is normal for ReducedDeltaF markers but
low for TotalF, so confirm which marker type the case intends before scaling up.
