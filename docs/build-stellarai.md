# Building XGC on STELLAR-AI (stellarai)

Status 2026-09-22: dependency stack installed, XGC built from XGC-Devel branch
`stellarai_cpu_setup` with `XGC_PLATFORM=stellarai_cpu`, compute-node smoke
test `d0008sa` passed (job 283: 10 steps, `XGC finished!`, rc 0). No production
executable is selected yet.

STELLAR-AI (`stellarai-amd.princeton.edu`, Princeton RC / PPPL) is a CPU-only
cluster: 95 nodes, 2x AMD EPYC 9475F (Zen 5 "Turin", 96 cores per node, no SMT),
370 GB per node, Slurm 26.05, partition `cpu` (7-day limit plus a 5-minute
`OverTimeLimit`), account `ptheory`.

## Filesystems: the rule that shapes everything else

| Filesystem | Login node | Compute nodes | Use |
|---|---|---|---|
| `/projects` (tigerdata NFS, includes `/projects/XGC`) | yes | **no** | Dependency install, build only |
| `/scratch/gpfs/PTHEORY` (GPFS group fileset, 5 TiB) | yes | yes | XGC checkout, build, runs |
| `/home` (NFS, 100 GB) | yes | not relied on | nothing XGC-related |

There is no per-user scratch; use `/scratch/gpfs/PTHEORY/<user>`. The
`/projects` restriction was found the hard way: smoke-test jobs 274-277 died in
0-1 s because the env script under `/projects` did not exist on compute node
`sai-n04n9`, and other users' jobs started from `/projects/M3DC1` died the
same way. Hence two design choices below: all dependencies are **static**, and
batch jobs source the env file from the **scratch checkout**, not from
`/projects`.

## Paths

| Item | Path |
|---|---|
| Env file (used by jobs and builds) | `<checkout>/quickstart/modules_and_env/stellarai_cpu.sh` |
| Installed copy of the env file | `/projects/XGC/STELLARAI/Software/bin/set_up_xgc.stellarai` (login node only; keep identical) |
| Dependency build script | `/projects/XGC/STELLARAI/Software/bin/build_deps.stellarai [ninja\|fftw\|kokkos\|adios2\|gtest\|petsc\|all]` |
| Dependency sources, logs | `/projects/XGC/STELLARAI/Software/src/`, `src/logs/` |
| Dependency install root | `/projects/XGC/STELLARAI/Software/install/<pkg>/<version>/gcc-14.3-openmpi-5.0.10`, with `<version>/DEFAULT` and `<pkg>/DEFAULT` links (same layout as `/projects/XGC/STELLAR/Software`) |
| XGC checkout | `/scratch/gpfs/PTHEORY/seungku/XGC-Devel`, branch `stellarai_cpu_setup` |
| Build dir, executable | `/scratch/gpfs/PTHEORY/seungku/XGC-Devel/build`, `build/bin/xgc` (57 MB) |
| Platform file | `CMake/find_dependencies_stellarai_cpu.cmake`; quickstart files `quickstart/{modules_and_env,cmake_configs,job_scripts,runlines}/stellarai_cpu.sh`; section in `Docs/cmake_build_instructions.rst` |

The login node has no GitHub key, so the branch is pushed from the Mac. The
checkout has the branch checked out, so push to a temporary ref and reset:

```bash
git push -f stellarai:/scratch/gpfs/PTHEORY/seungku/XGC-Devel stellarai_cpu_setup:refs/heads/sync_tmp
```

```bash
ssh stellarai 'cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel && git reset --hard sync_tmp && git branch -D sync_tmp'
```

## Toolchain and dependency stack

Site modules, loaded by the env file: `gcc/14` (gcc 14.3.1),
`openmpi/gcc/5.0.10` (HPC-X based), `hdf5/gcc/openmpi-5.0.10/1.14.6`.
Everything else is built from source with `-O3 -march=znver5`, all static:

| Package | Options |
|---|---|
| ninja 1.13.1 | build tool only |
| FFTW 3.3.10 | static only (`--disable-shared`), OpenMP, threads, MPI, AVX2, AVX-512 |
| Kokkos 4.7.04 | Serial + OpenMP, `Kokkos_ARCH_ZEN5`, static, Release |
| ADIOS2 2.12.1 | static, MPI, Fortran, SST; no HDF5, Python, ZeroMQ, DataMan, MGARD |
| gtest 1.17.0 | with gmock |
| PETSc 3.24.4 | static, `--download-fblaslapack --download-metis --download-parmetis --download-zlib`, OpenMP, threadsafety, Fortran bindings, `--with-mpiexec=srun` |

Not installed: Cabana (not referenced by XGC's CMake), FusionIO and DEGAS2
(not wanted for now), Ginkgo, Omega_h. camtimers and pspline are built from the
XGC tree (`USE_SYSTEM_CAMTIMERS/PSPLINE=OFF`), as on `perlmutter_cpu`.

`build_deps.stellarai` reuses a source directory when it exists and logs
`SOURCE REUSED` or `SOURCE DOWNLOAD`. PETSc's `--download-*` packages are
fetched again whenever its arch directory is removed. A full `all` rebuild
takes about 17 minutes on the login node at `J=16`.

## Build

```bash
source /scratch/gpfs/PTHEORY/seungku/XGC-Devel/quickstart/modules_and_env/stellarai_cpu.sh
cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel
rm -rf build && mkdir build && cd build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 16 xgc          # 680 targets, about 7 minutes
```

The env file sets `CC/CXX/FC` to the OpenMPI wrappers and
`XGC_PLATFORM=stellarai_cpu`, and puts ninja on `PATH`. Library paths are
hard-coded in the platform file. Do not pass compilers on the cmake line.

### Pitfall: `mpif.h: No such file or directory` in camtimers

OpenMPI 5's `mpifort` drops its `-I` flags when invoked with `-E` (check with
`mpifort --showme -cpp -E x.F90`). Ninja preprocesses Fortran in a separate
`-E` step, so `#include <mpif.h>` in `libs/camtimers/perf_mod.F90` fails. The
platform file adds `-I$MPI_HOME/include` to `CMAKE_Fortran_FLAGS`; `MPI_HOME`
comes from the openmpi module. If the error returns, the shell that ran cmake
did not load the modules. Delete `build/` and configure again.

### Pitfall: stale `FFTW3Config.cmake`

FFTW builds in its source tree, and `make distclean` does not remove the
generated `FFTW3Config.cmake`. Reinstalling into a new prefix then installs a
config file that points at the old prefix. `build_deps.stellarai` deletes it
before configuring.

## Running

- **Env file**: source `<checkout>/quickstart/modules_and_env/stellarai_cpu.sh`
  (xgc-automation: `xgc.env_script` in `machines.local.yaml`). The copy under
  `/projects` fails on compute nodes.
- **Layout**: 96 cores per node, no SMT, so ranks x `OMP_NUM_THREADS` = 96.
  12 ranks x 8 threads is the tested layout.
- **`srun -c 8 --cpu-bind=cores` is required**: Slurm 26.05 does not pass
  `sbatch --cpus-per-task` on to `srun`, and its default bind mask covers a
  whole socket. With `OMP_PROC_BIND=spread` the six ranks on a socket then put
  their threads on the same 8 cores and the other 40 sit idle. Jobs 281 and 282
  used about 1.3 cores per rank; 281 timed out after 4 of 10 steps. xgc-automation
  sets `scheduler.srun_cpu_binding: true` for this machine; the XGC-Devel
  quickstart run line already passes `-c` and `--cpu-bind=cores`.
- **Env file stderr**: job scripts run with `set -euo pipefail`. The env
  file's stderr goes to `env_setup.err` in the run directory, which is where
  the `/projects` failures were read from.
- **Runtime libraries**: the executable needs only `/lib64`,
  `/usr/local/openmpi/5.0.10/gcc` and `/usr/local/cuda` (pulled in by the HPC-X
  OpenMPI). `ldd` finds all of them without any module loaded.

## Verification

Checked 2026-09-22 on the static build (commit `c60540b6`):

| Check | Result |
|---|---|
| Compiler identity (`CMakeCache.txt`) | OpenMPI 5.0.10 wrappers over gcc 14.3.1, `CMAKE_BUILD_TYPE=Release` |
| Optimisation flags | C++: `-O3 -DNDEBUG -fopenmp -march=znver5 -mtune=znver5` (from Kokkos). Fortran: `-O3` only |
| Build | 680/680 targets, `bin/xgc` 57 MB |
| `ldd` with an empty environment | 0 not found, 0 libraries under `/projects`; RUNPATH is only the OpenMPI lib dir |
| Login-node `xgc --dryrun -n_ranks 12` | passed on the `xgc1example-stellarai` input |
| Compute-node smoke test | `d0008sa` job 283 on `sai-n04n9`: 1 node, 12 ranks x 8 threads, `srun -n 12 -c 8 --cpu-bind=cores`. 10/10 steps, `XGC finished!`, exit 0, 1 min 41 s wall. ADIOS2 BP output written (`xgc.3d`, `xgc.f3d`, `xgc.oneddiag`, mesh and equilibrium files). Each rank ran on its own 8 cores at ~97% per thread; collisions took 91 s against 639 s in job 281 without core binding |

The smoke-test input is the old `/projects/XGC/STELLAR/BENCH/XGC1Example.tar.gz`
example migrated to the current code (kb case `xgc1example-stellarai`). The
dryrun accepted three problems that only the real run rejected: a parameter in
the wrong namelist (`f0_update_alpha` belongs in `&f0_param`), missing empty
namelists (`&resamp_param`, `&init_cond`, `&rad_param`), and source parameters
that are now per-species arrays (`src_narea` and friends need one value per
species). All three are fixed in the case and recorded there.

## Open items

- Select a production executable (the smoke test passed; nothing production-sized has run yet).
- Perlmutter and stellar use the same Slurm template without `srun -c`. Check
  their thread placement (as above, with `srun --overlap` into a running job)
  before enabling `srun_cpu_binding` there.
- Add `-march=znver5` to the Fortran flags if profiling shows the Fortran
  kernels matter.
- Give the login node a GitHub deploy key so the checkout can `git fetch`.
- Merge `stellarai_cpu_setup` into XGC-Devel `master` once a smoke test passes.
