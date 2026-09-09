# Running (and building) XGC on Aurora

Status 2026-09-09: live round trip passed. The user's own build
(`XGC-Devel/build/bin/xgc`, branch `dadt_prediction` at `eb43591a`, IntelLLVM
2025.3.2, 26.26.0 PE) ran the ECBC electromagnetic smoke test on 2 `debug`
nodes: 24 ranks, 20 steps, `Exit_status=0`, about 2.5 minutes wall time, 4.3 GB
host and 19-28 GB device memory per rank, dAdt NN-input files written
(job `8814507`). Cancel is still
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

## Login-node dryrun

Not possible. `xgc --dryrun` on an Aurora login node dies in `MPI_Init`
("Fatal error in internal_Init: Other MPI error", exit 15) before reading the
input: the login nodes have no PALS launcher context and `sycl-ls` finds no
GPU platform. As on Polaris, validate inside a 1-node `debug` job, or submit
without a dryrun and read the first minute of the job stdout; XGC
rejects a bad namelist within seconds, before particle loading.

## Traps met on the first round trip

- `select=N:system=aurora` is rejected by `qsub` ("Resource invalid in
  select specification: system") even though `pbsnodes` advertises the
  resource. Render `select=N` only.
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

Build automation is deliberately manual. The XGC quickstart configure
for Aurora is `cmake_configs/aurora.sh` after sourcing
`modules_and_env/aurora.sh`; it uses `mpic++ -cxx=icpx`, `mpifort -fc=ifx`,
`-fsycl` device code for `pvc`, and `-DUSE_GPU_AWARE_MPI=On`. Record the
commit and the dependency modules that were loaded when it is done.
