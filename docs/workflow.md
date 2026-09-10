# Submission workflow

The path from an input file to a running job, and the checks in between.

```
  plan_simulation_id preview global ID and directory         local, free
       |
  [human approves]   approve the exact sequential number
       |
  reserve_simulation_id record ID; advance sequence          local, free
       |
  physics_advisor   natural language -> evidenced baseline   local, free
       |
  list_cases        inspect validated baselines              local, free
       |
  stage_simulation_case copy baseline to canonical directory seconds
       |
  plan_input        apply overrides, return a diff           local, free
       |
  [human approves]  review the diff
       |
  write_input       write + xgc --dryrun                     ~3 s, free
       |
  plan_job          render script, estimate node-hours       local, free
       |
  [human approves]  review script + cost + dryrun report
       |
  submit_job        re-runs dryrun, then sbatch/qsub         consumes allocation
       |
  job_status        pending -> running -> completed
       |
  job_output        tail of stdout/stderr
```

Long runs are normally sized to hit the wall clock and restart from a
checkpoint, so one simulation spans several jobs. A timed-out run leaves the
simulation `interrupted` rather than `failed`, and its status is derived from
the whole restart chain. `xgc-sim refresh` polls everything still in flight in
one command.

`simulation_id` identifies the scientific simulation and appears at the start
of its directory name. The timestamp `run_id` remains separate because one
simulation may have multiple submit/restart attempts. See
`docs/simulation-ids.md`.

Each step above also maintains the simulation's catalog record at
`kb/simulations/<simulation_id>.yaml`: reservation opens it, staging records
the directory, submission appends the run and the input hash, and `job_status`
marks it completed or failed. That record is what makes past work findable
later, with `xgc-sim search` or `search_simulations`. See
`docs/simulation-catalog.md`.

`physics_advisor` is deliberately conservative. It selects only from validated
cases in `kb/cases/`, applies explicit deterministic rules, and returns
unsupported when the knowledge base does not cover the requested physics. A
numerical override is never inferred from model intuition; only a value
explicitly stated by the user and covered by a rule can be proposed. The
proposal still goes through `plan_input`, which validates it against the XGC
source metadata and produces the diff required for human review.

## Building an input

Inputs are built by modifying a validated baseline, not written from scratch.
XGC has 708 input parameters and most are coupled, so starting from a case
known to pass the dryrun is both safer and easier to review.

```python
plan_simulation_id(["stellar"], "ecbc short")
reserve_simulation_id(["stellar"], "ecbc short",
                      expected_number=645, confirm=True)
stage_simulation_case("ecbc", "n645st", confirm=True)
plan_input("ecbc", {"sml_mstep": 200, "sml_restart": False},
           target_path=".../n645st_ecbc_short/input")  # returns a diff
write_input(..., confirm=True)                   # writes, then validates
```

Every override is checked against XGC's parameter metadata before anything is
written. Unknown names (with `did_you_mean` suggestions), invalid enum values,
and type mismatches are all rejected up front. Booleans and strings are
rendered in Fortran form automatically, so `False` becomes `.false.` and a
string is quoted.

Overrides are applied by editing the file in place, so comments, ordering, and
spacing survive. That keeps the diff small enough to actually review.

`write_input` refuses to overwrite a baseline case directory. Baselines are
references and need to stay reproducible.

### Parameter metadata

`describe_parameter` reports a parameter's type, default, description, and
valid options, generated from the XGC C++ source on the target machine. It
reflects the version actually installed there, so it is the right thing to
consult instead of recalling semantics from memory.

This is the same metadata the XGC input GUI uses -- both go through
`utils/input_gui/`, so a file written by these tools opens cleanly in the GUI
and vice versa.

The metadata is cached under `.cache/`, keyed by the XGC commit, so it
refreshes automatically when the source changes.

**Units are a trap here.** Several inputs are given in normalized form and
converted internally: `sml_dt` is in main-ion toroidal transit times (not
seconds), and the `psi` inputs are in normalized poloidal flux. Read the
description rather than assuming SI.

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
   be used when the user explicitly asks. On machines whose login nodes cannot
   run `xgc --dryrun` at all the gate takes the parallel form described next.

### Dryrun on machines without a login-node dryrun

Polaris (CUDA build, no visible GPU, NVML broken on login nodes) and Aurora (no
launcher on a UAN) cannot run `xgc --dryrun` before submission. Blocking on it
would mean either a hand-made debug job before every submission or
`skip_dryrun`, and in practice it meant the latter. So on machines with
`dryrun.login_node: false` in `machines.yaml` the rule is:

1. Submit the production job first. Nothing about the input has been checked
   yet, and the plan says so (`dryrun: null`, `dryrun_parallel: {...}`).
2. Wait `dryrun.start_grace_seconds` (20 s) and ask the scheduler about the
   job. If it is already running, the run itself is the validation; no second
   job is spent (`dryrun_parallel.started: false`).
3. Otherwise submit a 1-node job in `dryrun.queue` (`debug`, 15 min) that runs
   `xgc --dryrun -n_ranks <production ranks>` in the same case directory, with
   the same executable and environment script. Its script is
   `<case_dir>/dryrun-<run_id>.sh`; its output `pbs-<jobid>.out`.
4. The next `job_status` or `xgc-sim refresh` reads that output once the debug
   job has finished, parses it exactly like a login-node dryrun, and attaches
   the verdict to the run (`dryrun_parallel.report` in the catalog record and
   in `provenance.json`). A rejection is reported as a **warning naming the
   production job**; nothing is cancelled automatically, because cancelling
   is a human decision (absolute rule 1).

`skip_dryrun=True` is therefore no longer needed on Polaris or Aurora. The
debug queue allows one job per user, so a second submission while a dryrun
job is still queued reports `dryrun_parallel.error` and the run proceeds
unvalidated; the status check will say so.

Always pass `case_dir` for real XGC runs. It is unnecessary for `hello_world`
jobs, which do not read an input file.

### Provenance classes

New local submission records are separated by intent:

- `runs/master/<run_id>/` contains curated scientific provenance. A registered
  simulation with a `simulation_id` is always stored here. `master` describes
  the provenance tier, not the current Git branch.
- `runs/test/<run_id>/` contains development, unregistered, and scheduler
  smoke-test provenance. `hello_world` jobs are always stored here, and this
  directory is ignored by Git.

An intentional scientific run without a `simulation_id`, such as a historical
case that cannot yet be registered, must explicitly request
`provenance_class="master"`. Existing records directly below `runs/<run_id>/`
remain readable as legacy provenance; new records are never written there.

Runs that predate the catalog can be folded into it with `xgc-sim adopt-runs`,
which groups them by working directory so restarts stay together. See
`docs/simulation-catalog.md`.

Each record also holds a copy of the input namelist actually submitted, plus
its SHA-256 in `input_sha256`. The remote case directory is routinely edited
for the next attempt, so without the copy a record could say which job ran but
not what it ran.

### Build banner capture

Every XGC binary prints a banner at startup -- build date, host, compiler,
build type, and a `GIT INFO` block naming the exact commit and whether the
build tree was dirty. That banner is the ground truth about the binary that
actually ran, as opposed to the one the plan referenced, so the first status
check (or `xgc-sim refresh`) after a job starts reads a bounded slice of its
stdout, parses the banner (`tools/build_banner.py`), and stores it:

- the full banner in the run's `provenance.json` under `xgc.build`, with
  `xgc.build_commit_mismatch` flagged when the observed commit differs from
  the one the plan recorded (the binary was rebuilt in between);
- a compact summary (commit, subject, build date/type, compiler, dirty state,
  origin URL) in the catalog's run entry under `build`, which makes
  `xgc-sim search <commit>` answer "what ran this build".

Capture happens once per run. Restart attempts share a case directory, so the
stdout file is selected by job id (`slurm-<jobid>.out`, `<name>.o<jobid>`); a
job whose completed stdout has no banner (a non-XGC job) is marked so refresh
stops re-reading it. A dirty build's banner also names the changed files and
the `git_diff_XGC_core.txt` preview XGC writes into the run directory.

### Recommended future gate: executable availability

Before a scientific job is submitted, the runner should verify the exact remote
executable referenced by the rendered job script when its path was supplied or
overridden externally, or when a significant amount of time has passed since
the last successful validation. The check may run during planning or another
pre-submit stage; it does not need to occur immediately before `sbatch` or
`qsub`.

The pre-submit check should:

- resolve the final executable after all machine configuration and request
  overrides have been applied;
- require an absolute remote path for scientific jobs;
- verify on the target machine that the path exists, is a regular file, and is
  executable;
- source the configured environment script first when executable validation
  depends on that environment;
- ensure that the checked path is identical to the path embedded in the job
  script and provenance record;
- block submission with an actionable error if validation fails; and
- record the executable path and, when practical, its size, modification time,
  checksum, or corresponding XGC commit in provenance.

The submission provenance records the executable, environment script, and XGC
commit selected by the reviewed plan. When an executable is overridden, its
commit must be supplied explicitly; the configured default build's commit is
not attributed to a different binary.

A recent successful validation or dryrun may be reused when the executable path
and relevant file metadata remain unchanged. An older result should not be
trusted indefinitely because the binary may have been removed or rebuilt.
Scheduler-only `hello_world` jobs are exempt because they do not launch XGC.

For PBS machines, the `partition` argument is interpreted as the PBS queue.
Polaris defaults to `debug-scaling`; its PBS script also requires the
configured account, system, and filesystem list. Missing required PBS
resources are rejected during `plan_job`, before any remote write or `qsub`.

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

ECBC electrostatic delta-f, core simulation, n=6. Recorded in
`kb/cases/ecbc.yaml`; its `case_dir` is the original author's stellar home
directory. Another user who cannot read it should follow the reconstruction
options the tools print (or see `docs/setup-new-user.md` §5) and rebind via
`kb/cases/ecbc.local.yaml`.

**Status: passes cleanly** (0 removed, 0 deprecated, 0 warnings) as of
2026-07-28 against XGC-Devel `39c5c89f` (the commit recorded in the case
YAML). At 64 ranks: 0.146 GB per rank against 375.9 GB available, 0.0517
particles per 5D vertex.

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
