# Aurora operations: submit, DAOS, and reading results

The day-to-day recipe for running XGC on Aurora with this toolchain. Machine
facts and the build live in `docs/build-aurora.md`; the generic submission
loop is `docs/workflow.md`. Everything below was exercised live on
2026-09-09/10 (jobs 8814507 on flare and 8814705 in DAOS).

## 0. Connect

Aurora is an MFA host. Open the session yourself; the tools reuse it.

```bash
ssh aurora          # MobilePASS+ passcode; the ControlMaster persists 8 h
ssh -O check aurora # "Master running" means the tools can work
```

If a tool reports `Control socket connect ... No such file or directory`,
the master is gone (the session was closed, or 8 h passed): run `ssh aurora`
again. Nothing in the queue is affected by a dropped session.

## 1. Where things live

| Item | Path |
|---|---|
| Project space (Lustre "flare", jobs must launch from here) | `/lus/flare/projects/TokamakProfile/shku` |
| XGC checkout and build | `.../shku/XGC-Devel`, executable `build/bin/xgc` |
| Runtime environment script | `.../XGC-Devel/quickstart/modules_and_env/aurora.sh` |
| Mesh, equilibrium, profile inputs | `.../shku/XGC_inputs` (51-character path, see the 65-character limit below) |
| DAOS pool / container | `TokamakProfile` / `dadt_predict`, mounted at `/tmp/TokamakProfile/dadt_predict` inside jobs |
| Flare smoke test | `.../shku/dadt_predict/test_ecbc_small` (job 8814507) |
| DAOS smoke test | `.../shku/dadt_predict/test_ecbc_small_daos` (job 8814705) |
| dAdt production case | `.../shku/dadt_predict/run1_base` (`xgcs` renders the DAOS variant) |

## 2. Queues and sizes

| Queue | Nodes | Wall time | Use |
|---|---:|---:|---|
| `debug` | 1-2 | <= 1 h | smoke tests; one job per user at a time |
| `debug-scaling` | 2-256 | <= 1 h | scaling checks |
| `capacity` | 1-16 | <= 7 days | production below 256 nodes (the dAdt 8-node run) |
| `prod` | >= 256 | <= 24 h | large production only; smaller requests are rejected |

One Aurora node runs 12 XGC ranks (one per GPU tile) with 16 threads each.
XGC needs the total rank count divisible by `sml_nphi_total`; with 16 planes
that means a multiple of 16 ranks, so 4, 8, 12, 16 ... nodes. A 2-node test
uses `sml_nphi_total=8` and must lower `sml_mode_select_nmax` below
`nphi/2`, otherwise XGC aborts at startup.

## 3. Submitting through the tools (recommended)

The same loop as Polaris. A registered scientific run:

```text
plan_simulation_id(["aurora"], "dadt ecbc em")       # preview the number
reserve_simulation_id(["aurora"], ..., confirm=True)  # n65Xau
stage_simulation_case("dadt-ecbc-em", "n65Xau", confirm=True)
plan_input(...) / write_input(..., confirm=True)      # edits with a diff
plan_job("aurora", simulation_id="n65Xau", nodes=8, walltime="06:00:00",
         partition="capacity", daos=True)             # script + node-hours
submit_job("aurora", simulation_id="n65Xau", case_dir=<dir>,
           partition="capacity", daos=True, confirm=True)
job_status / job_output / xgc-sim refresh             # as on Polaris
```

Notes that differ from Polaris:

- No login-node dryrun: `xgc --dryrun` cannot run on an Aurora login node (no
  PALS launcher, no GPU platform: it dies in `MPI_Init`). `submit_job` therefore
  submits the production job first and, if it is still queued after 20 s, runs
  the dryrun as a parallel 1-node `debug` job (`dryrun:` in `machines.yaml`);
  the verdict appears on the next `job_status` / `xgc-sim refresh` as
  `dryrun_parallel`. `skip_dryrun=True` is no longer needed. The first minute
  of `pbs-<jobid>.out` remains a second check: a bad namelist is rejected there
  before particle loading.
- Omit `daos=True` to run on flare. The rendered script is identical except
  for the DAOS block.
- The rendered script (`submit.sh`) and the logs `pbs-<jobid>.out/.err` are
  written into the case directory on flare in both modes.
- `xgc-queue-watch` and `xgc-sim refresh` cover Aurora once the session is
  open; they skip it with a reason when it is not.

## 4. Submitting by hand

`run1_base/xgcs` is a rendered script and can be edited and submitted like
any PBS script. Always submit from the case directory on flare:

```bash
cd /lus/flare/projects/TokamakProfile/shku/dadt_predict/run1_base
qsub xgcs
qstat -u shku
```

The directives that matter on Aurora:

```bash
#PBS -l select=8            # no ":system=aurora": qsub rejects it
#PBS -l place=scatter
#PBS -l walltime=06:00:00
#PBS -l filesystems=home:flare:daos_user_fs   # drop :daos_user_fs for flare runs
#PBS -A TokamakProfile
#PBS -q capacity
#PBS -k doe
```

and the launcher for 12 ranks per node:

```bash
mpiexec --np $((8*12)) -ppn 12 \
  --cpu-bind=list:1-8,105-112:9-16,113-120:17-24,121-128:25-32,129-136:33-40,137-144:41-48,145-152:53-60,157-164:61-68,165-172:69-76,173-180:77-84,181-188:85-92,189-196:93-100,197-204 \
  --gpu-bind=list:0.0:0.1:1.0:1.1:2.0:2.1:3.0:3.1:4.0:4.1:5.0:5.1 \
  --rlimits CORE -genvall \
  --no-vni --env LD_PRELOAD=/usr/lib64/libpil4dfs.so \   # DAOS only
  /lus/flare/projects/TokamakProfile/shku/XGC-Devel/build/bin/xgc
```

Keep `MPIR_CVAR_ENABLE_GPU=1`: the build is GPU-aware
(`-DUSE_GPU_AWARE_MPI=On`), and `0` produced a GPU page fault at startup in
the March scripts. Hand-submitted jobs are not in the catalog; use section 3
when the run should be findable later.

## 5. DAOS: what the job does and what you must do

What the rendered script does for you:

1. `module use /soft/modulefiles; module load daos`
2. `launch-dfuse.sh TokamakProfile:dadt_predict` mounts the container on
   every node at `/tmp/TokamakProfile/dadt_predict`.
3. Copies `input`, `petsc.rc`, `adios2cfg.xml` from the flare case directory
   into `/tmp/TokamakProfile/dadt_predict/<case name>` and runs XGC there.
   That directory persists in the container between jobs, so
   `restart_dir/` and `timestep.dat` are found on the next submission and a
   restart chain needs no extra step.
4. Runs with `--no-vni` and the `libpil4dfs.so` interception library.
5. The PBS epilogue unmounts. Logs stay on flare.

What you must do:

- **`sml_input_file_dir` must be absolute** and at most 65 characters, e.g.
  `'/lus/flare/projects/TokamakProfile/shku/XGC_inputs/'`. Relative paths
  point into the container and fail. `submit_job(daos=True)` checks this and
  refuses to submit; a hand-submitted job only fails after it starts.
- Edit `input` on flare, not in the container. Each job copies the flare
  copy in, so a restart with a changed `input` works the usual way.
- Treat the container as scratch. ALCF still calls DAOS a test
  configuration that can be wiped with notice; copy what must survive to
  flare (section 7).

Health checks when something looks wrong (login node, `module load daos`):

```bash
daos pool query TokamakProfile                    # state=Ready, free space
daos cont get-prop TokamakProfile dadt_predict    # Health (status) HEALTHY
daos cont list TokamakProfile
```

## 6. Reading results after the run

On flare (`test_ecbc_small`, or any run without `daos=True`) the outputs are
ordinary files in the case directory; use the analysis tools or
`xgc-remote-gui` as on Polaris.

In DAOS the outputs are invisible until the container is mounted. On a login
node:

```bash
module use /soft/modulefiles && module load daos
MNT=/tmp/$USER/TokamakProfile/dadt_predict
mkdir -p $MNT
start-dfuse.sh -m $MNT --pool TokamakProfile --cont dadt_predict
ls $MNT/<case name>                 # xgc.oneddiag.bp, xgc.3d.*.bp, restart_dir, ...
```

Read with the XGC environment's Python (adios2 2.11 bindings are loaded by
`modules_and_env/aurora.sh`):

```bash
source /lus/flare/projects/TokamakProfile/shku/XGC-Devel/quickstart/modules_and_env/aurora.sh
python3 -c "import adios2; f=adios2.FileReader('$MNT/test_ecbc_small_daos/xgc.oneddiag.bp'); print(len(f.available_variables()))"
```

Always unmount when done; a dfuse left behind on a login node is the one
thing ALCF asks users not to do:

```bash
fusermount3 -u $MNT
```

`/lus/flare/projects/TokamakProfile/shku/dadt_predict/mount_check.sh` does
mount, list, read, unmount in one go and is the template for a longer
session. The mount is per login node and per session; `aurora.alcf.anl.gov`
round-robins, so the ControlMaster pins one node and the tools see the same
mount you made in that session.

## 7. Moving data out of DAOS

Small products (1D diagnostics, summaries, plots): copy through the login
node mount with `cp` to flare or home. Bulk data (`restart_dir`, `xgc.3d`,
`xgc.f3d`, `xgc.dAdt_nn_input.*`): use the DAOS data mover from a compute
job, not `cp` on a login node, following the ALCF "Moving data to Aurora"
page (`mpifileutils` `dcp` between `/tmp/<pool>/<container>` and flare).
Reduce remotely first and pull only summaries locally (AGENTS.md data
principles).

## 8. Known traps

| Symptom | Cause | Fix |
|---|---|---|
| `qsub: Resource invalid in "select" specification: system` | `:system=aurora` in the chunk | use `select=N` only |
| `Fatal error in internal_Init` from `xgc --dryrun` on a login node | no launcher on UANs | validate inside a job |
| `sml_mode_select_nmin=0 and sml_mode_select_nmax>=sml_nphi_total/2 is not supported` | test shrank `nphi` | lower `sml_mode_select_nmax` |
| `Segmentation fault from GPU ... access: Read` at startup | `MPIR_CVAR_ENABLE_GPU=0` with a GPU-aware build | set it to 1 |
| 17 shared libraries `not found` | binary predates a PE upgrade | rebuild (`xgc-build` skill, `docs/build-aurora.md`) |
| Input files not found inside DAOS | relative `sml_input_file_dir` | absolute flare path, <= 65 characters |
| `Failed to connect to container` / `DER_NOTSUPPORTED` on the handles file | non-ALCF mount helper | use `launch-dfuse.sh` |
