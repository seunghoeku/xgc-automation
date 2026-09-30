# Building XGC on STELLAR-AI (stellarai-amd.princeton.edu)

Verified: 2026-09-22 / XGC-Devel `stellarai_cpu_setup` @ `e3b1a0b0` (code; later
commits are docs and the quickstart.sh build step; the binary banner says `c60540b6`,
the same tree before a mode-only rewrite), built in
`/scratch/gpfs/PTHEORY/seungku/XGC-Devel/build`. Full notes: `docs/build-stellarai.md`.

## The one rule: `/projects` is login-node only

`/projects` (tigerdata NFS, including `/projects/XGC`) is not mounted on the
compute nodes. Consequences:

- Dependencies live in `/projects/XGC/STELLARAI/Software/install`, but all are
  **static**, so `bin/xgc` needs nothing from `/projects` at run time.
- XGC checkout, build and run directories go on `/scratch/gpfs/PTHEORY/<user>`.
- Jobs source the env file **from the scratch checkout**
  (`quickstart/modules_and_env/stellarai_cpu.sh`), never the installed copy
  `/projects/XGC/STELLARAI/Software/bin/set_up_xgc.stellarai_cpu`.

## Summary

```bash
source /scratch/gpfs/PTHEORY/seungku/XGC-Devel/quickstart/modules_and_env/stellarai_cpu.sh
cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel
rm -rf build && mkdir build && cd build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 16 xgc          # 680 targets, ~7 min; build/bin/xgc (57 MB)
```

CPU-only (2x AMD EPYC 9475F, Zen 5, 96 cores/node, no SMT, no GPUs), so
`USE_GPU=OFF` and `xgc --dryrun` runs on the login node.

## Environment

| Item | Value |
|---|---|
| Compiler | gcc 14.3.1 via OpenMPI 5.0.10 wrappers (`mpicc`/`mpicxx`/`mpifort`) |
| Modules | `gcc/14`, `openmpi/gcc/5.0.10`, `hdf5/gcc/openmpi-5.0.10/1.14.6` |
| Dependencies (static) | `/projects/XGC/STELLARAI/Software/install/<pkg>/DEFAULT/DEFAULT`: FFTW 3.3.10, Kokkos 4.7.04 (OpenMP+Serial, ZEN5), ADIOS2 2.12.1, gtest 1.17.0, PETSc 3.24.4 (fblaslapack/metis/parmetis); ninja 1.13.1 |
| Dependency rebuild | `/projects/XGC/STELLARAI/Software/bin/build_deps.stellarai_cpu [pkg\|all]`, logs in `src/logs` |
| Platform | `XGC_PLATFORM=stellarai_cpu` -> `CMake/find_dependencies_stellarai_cpu.cmake` (branch `stellarai_cpu_setup`, not in master) |
| camtimers/pspline | built in-tree (`USE_SYSTEM_*=OFF`) |
| Not installed | FusionIO, DEGAS2, Cabana, Ginkgo, Omega_h |

Do not pass compilers on the cmake line; the env file sets `CC/CXX/FC` and
`XGC_PLATFORM`. Library paths are hard-coded in the platform file.

## Pitfalls

- **`Fatal Error: mpif.h: No such file or directory` (camtimers)**: OpenMPI 5's
  `mpifort -E` drops its `-I` flags and Ninja preprocesses Fortran with `-E`.
  The platform file adds `-I$ENV{MPI_HOME}/include`. If it returns, cmake ran
  without the modules loaded; delete `build/` and configure again.
- **Stale `FFTW3Config.cmake`** after reinstalling FFTW to a new prefix:
  `make distclean` leaves it in the source tree. The build script deletes it.
- **Job dies in 0-1 s, `env_setup.err`: env script "No such file or
  directory"**: the job sourced the `/projects` copy. Use the scratch checkout.
- **Ranks use ~1.3 of 8 cores**: `srun` needs `-c 8 --cpu-bind=cores`. Slurm
  26.05 does not pass `sbatch --cpus-per-task` on, and its default mask covers a
  whole socket, so with `OMP_PROC_BIND=spread` six ranks stack their threads
  on the same 8 cores. xgc-automation: `scheduler.srun_cpu_binding: true`.

## Dryrun is not enough for old inputs

The old `/projects/XGC/STELLAR/BENCH/XGC1Example.tar.gz` input passed
`xgc --dryrun` but the real run still rejected it three times: a parameter in
the wrong namelist (`f0_update_alpha` belongs to `&f0_param`), missing empty
namelists (`&resamp_param`, `&init_cond`, `&rad_param`), and per-species
arrays given one value (`src_narea` and the other `src_*` parameters). The
fixed copy is the kb case `xgc1example-stellarai`.

## Runtime

Slurm 26.05, partition `cpu` (7 days + 5 min OverTimeLimit), account
`ptheory`, `SLURM_MPI_TYPE=pmix`. 12 ranks x 8 threads per node. Scratch is
the group fileset `/scratch/gpfs/PTHEORY/<user>`.
