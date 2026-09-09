# Running (and building) XGC on Aurora

Status 2026-09-09: live round trip passed. The user's own build
(`XGC-Devel/build/bin/xgc`, branch `dadt_prediction` at `eb43591a`, IntelLLVM
2025.3.2, 26.26.0 PE) ran the ECBC electromagnetic smoke test on 2 `debug`
nodes: 24 ranks, 20 steps, `Exit_status=0`, about 2.5 minutes wall time, 4.3 GB
host and 19-28 GB device memory per rank, dAdt NN-input files written
(job `8814507`, provenance `runs/test/run-20260909T190345Z/`). Cancel is still
unexercised. Note the banner reported an empty `BUILD TYPE`: the configure left
`CMAKE_BUILD_TYPE` unset, so the compiler defaults decided the optimization
level; set `RelWithDebInfo` or `Release` explicitly for production.

Official references:

- [Running jobs on Aurora](https://docs.alcf.anl.gov/aurora/running-jobs-aurora/)
- [Flare (Lustre) filesystem](https://docs.alcf.anl.gov/aurora/data-management/lustre/flare/)
- [XGC build instructions (Aurora section)](https://xgc.pppl.gov/html/cmake_build_instructions.html)
- [Running XGC](https://xgc.pppl.gov/html/running_xgc.html)
- XGC-Devel `quickstart/{modules_and_env,cmake_configs,job_scripts,runlines}/aurora.sh`

## What is different from Polaris

| Item | Polaris | Aurora |
|---|---|---|
| Node | 1 AMD CPU + 4 NVIDIA A100 | 2 Intel SPR CPUs (52 cores each, cores 0 and 52 reserved) + 6 Intel PVC GPUs, 2 tiles each |
| XGC ranks per node | 4 (one per GPU) | **12** (one per GPU tile) |
| Threads per rank | 16 | 16 (8 physical cores + hyperthreads) |
| GPU affinity | `CUDA_VISIBLE_DEVICES` wrapper | `--gpu-bind=list:0.0:0.1:...:5.1` or `gpu_tile_compact.sh` (`ZE_AFFINITY_MASK`) |
| CPU affinity | `--cpu-bind depth -d 16` | explicit `--cpu-bind=list:...` so no rank straddles the two sockets |
| GPU-aware MPI | off (no GTL) | **on**: `MPIR_CVAR_ENABLE_GPU=1`; XGC's Aurora cmake config sets `-DUSE_GPU_AWARE_MPI=On` |
| Compilers | GCC + CUDA (Cray PE) | oneAPI `icpx`/`ifx` + SYCL, `mpic++ -cxx=icpx` wrappers |
| Job filesystem | `/grand/<project>` | `/lus/flare/projects/<project>` (`filesystems=home:flare`); never launch from `$HOME` |
| Small-job queue | `debug-scaling` (1-10 nodes) | `debug` (1-2 nodes, <= 1 h) |
| Production queue | `prod` (>= 10 nodes) | `prod` requires **>= 256 nodes**; `capacity` (1-16 nodes, <= 7 days) is the sub-256 option |

The `prod` minimum is the trap most likely to bite a hand-written script: the
XGC quickstart example (`#PBS -l nodes=2`, `-q prod`) is rejected on
Aurora's current queue policy. The ALCF-recommended form is `-l select=N`
plus `-l place=scatter` and an explicit `-l filesystems=`.

## Queues (docs.alcf.anl.gov, 2026-09)

| Queue | Nodes | Wall time | Limits |
|---|---:|---:|---|
| `debug` | 1-2 | 5 min - 1 h | 1 job per user (running, accruing, or queued) |
| `debug-scaling` | 2-256 | 5 min - 1 h | 1 job per user |
| `prod` | 256-10624 | 5 min - 24 h | routing queue: small / medium / large |
| `capacity` | 1-16 | 5 min - 7 days | 2 running / 5 queued per user, 512 nodes total |

## Rendered profile (`templates/aurora.pbs.j2`)

- `#!/bin/bash -l`, `-l select=N` (a `:system=aurora` chunk resource is
  rejected by Aurora's qsub, unlike Polaris), `-l place=scatter`,
  `-l filesystems=home:flare`, `-k doe`, stdout/stderr redirected to
  `pbs-<jobid>.out/.err` in the run directory like Polaris.
- Sources the XGC environment script (`modules_and_env/aurora.sh`: kokkos,
  adios2, ginkgo modules, `XGC_PLATFORM=aurora`, bind lists, MPICH tweaks),
  then sets `OMP_NUM_THREADS` from `cpus_per_task`, `OMP_PROC_BIND=spread`,
  unsets `OMP_PLACES`, keeps `OMP_STACKSIZE` from the env script (1G default),
  and pins `ZE_FLAT_DEVICE_HIERARCHY=COMPOSITE` (tile binding needs it; the
  `frameworks` module switches it to FLAT).
- With 12 ranks per node it uses XGC's tested runline: explicit
  `--cpu-bind=list` (six ranks per socket, cores 49-52 left idle) and
  `--gpu-bind=list` over the twelve tiles, `--rlimits CORE -genvall`.
  Any other rank count falls back to `--depth=<threads> --cpu-bind depth`
  with the ALCF `gpu_tile_compact.sh` wrapper.
- `hello_world=True` runs `mpiexec ... hostname` for a scheduler smoke test.

## Login-node dryrun

Not possible. `xgc --dryrun` on an Aurora login node dies in `MPI_Init`
("Fatal error in internal_Init: Other MPI error", exit 15) before reading the
input: the login nodes have no PALS launcher context and `sycl-ls` finds no
GPU platform. As on Polaris, validate inside a 1-node `debug` job or submit
with `skip_dryrun=True` and read the first minute of `pbs-<jobid>.out`; XGC
rejects a bad namelist within seconds, before particle loading.

## Traps met on the first round trip

- `select=N:system=aurora` is rejected by `qsub` ("Resource invalid in
  select specification: system") even though `pbsnodes` advertises the
  resource. The Aurora template renders `select=N` only.
- Shrinking `sml_nphi_total` for a small test also caps the mode filter: XGC
  aborts with "sml_mode_select_nmin=0 and sml_mode_select_nmax>=sml_nphi_total/2
  is not supported". With 8 planes `sml_mode_select_nmax` must be at most 3
  (production: 16 planes, `nmax=7`).
- A March 2026 binary stopped linking after the PE moved to 26.26.0 (17
  unresolved libraries); rebuild after every Aurora PE update.
- `MPIR_CVAR_ENABLE_GPU=0` with a `-DUSE_GPU_AWARE_MPI=On` binary produces a
  GPU page fault at startup ("Segmentation fault from GPU ... access: Read").
  Keep it at 1 for XGC's Aurora build.

## Build notes

Build automation is out of scope (AGENTS.md). The XGC quickstart configure
for Aurora is `cmake_configs/aurora.sh` after sourcing
`modules_and_env/aurora.sh`; it uses `mpic++ -cxx=icpx`, `mpifort -fc=ifx`,
`-fsycl` device code for `pvc`, and `-DUSE_GPU_AWARE_MPI=On`. Record the
commit and the dependency modules that were loaded when it is done.

## Running in DAOS instead of flare

Aurora's DAOS (230 PB, up to 30 TB/s aggregate) is the fast tier; flare is
650 GB/s and shared. The TokamakProfile project has a pool (1.6 PB NVMe,
`daos pool query TokamakProfile`), and the POSIX container `dadt_predict` was
created in it on 2026-09-09 with ALCF's recommended redundancy (`rd_fac:3`,
`EC_16P3G32`, 2 MB chunks, crc32). XGC's restart dumps (one particle set per
rank, tens of GB each at production size) and the dAdt training output are
what make the bandwidth worth the extra steps.

Verified 2026-09-09 with the same ECBC smoke test as the flare run: job
`8814705`, 2 `debug` nodes, 24 ranks, 20 steps, `Exit_status=0`, 3 min 11 s
wall time, empty stderr, `SimpleFOM` 7.92e7 against 7.83e7 on flare (the
run is compute-bound at this size, so the equal figure says only that DAOS
cost nothing). The container now holds the 39 GB of output plus
`restart_dir/xgc.restart.00020.bp`, and the login-node mount below read it
back with `bpls`. Provenance: `runs/test/run-20260909T211140Z/`.

`plan_job(..., daos=True)` / `submit_job(..., daos=True)` render the DAOS
variant of the Aurora template:

1. `-l filesystems=home:flare:daos_user_fs` so the PBS prologue starts the
   DAOS agent on the compute nodes.
2. `module load daos` and `launch-dfuse.sh <pool>:<container>` mount the
   container with dfuse on every node at `/tmp/<pool>/<container>`.
3. `input`, `petsc.rc`, and `adios2cfg.xml` are copied from the flare case
   directory into `/tmp/<pool>/<container>/<case basename>`, and XGC runs
   there. The directory persists in the container across jobs, so
   `restart_dir/` and restart chains work exactly as on flare.
4. `mpiexec --no-vni --env LD_PRELOAD=/usr/lib64/libpil4dfs.so` runs XGC
   through the interception library (data and metadata bypass the kernel).
   `--no-vni` is mandatory for DAOS. For rank counts other than 12 per node
   the ALCF `gpu_tile_compact_LD_PRELOAD.sh` copy is used, because the stock
   tile wrapper's `udevadm` call misbehaves under the interception library.
5. Job stdout/stderr and `submit.sh` stay in the flare case directory, so
   `job_output`, the build-banner capture, and `xgc-queue-watch` need no
   mount. The PBS epilogue unmounts the container.

Constraints:

- `sml_input_file_dir` must be an **absolute flare path** because XGC's
  working directory is now the container mount, and XGC truncates the value
  at 65 characters (`character(len=65)` in `XGC_core/module.F90`).
  `/lus/flare/projects/TokamakProfile/shku/XGC_inputs/` is 51 characters.
  `submit_job` reads the input and refuses a relative or over-long value
  before `qsub`.
- DAOS is still called a test configuration by ALCF and may be wiped with
  notice. Treat the container as scratch: copy what must survive to flare.
- The user's earlier hand-written DAOS attempts (January 2026,
  `test/xgcs.test`) failed at the mount step (`Failed to connect to
  container`, `DER_NOTSUPPORTED` reading the handles file); they used a
  project-local mount helper. ALCF's `launch-dfuse.sh` (dfuse on the head
  node, `dbcast` of the handles, `clush` for the rest) is what the template
  uses.

### Reading results afterwards

The container is not visible on a login node until it is mounted there:

```bash
module use /soft/modulefiles && module load daos
mkdir -p /tmp/$USER/TokamakProfile/dadt_predict
start-dfuse.sh -m /tmp/$USER/TokamakProfile/dadt_predict --pool TokamakProfile --cont dadt_predict
ls /tmp/$USER/TokamakProfile/dadt_predict/<case>
# ... analysis, or copy reduced products to flare ...
fusermount3 -u /tmp/$USER/TokamakProfile/dadt_predict   # always unmount on login nodes
```

For bulk moves between DAOS and flare use the DAOS data mover
(`mpifileutils` `dcp`, see the ALCF "Moving data to Aurora" page) from a
compute job rather than `cp` through a login-node dfuse mount. The
`data_analyzer` tools take a remote directory path, so point them at the
login-node mount above; the mount is per login node and per session.
