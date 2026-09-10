# Building XGC on Polaris

Status 2026-09-09: the production binary is
`/grand/TokamakProfile/shku/XGC-Devel/bin/bin/xgc` (branch `dadt_prediction`,
commit `4cce91da`, built 2026-09-05) and it is what the `n648po`... production
runs use. Everything is built on the **CPE 26.03 stack** described here.

The two earlier builds this file used to describe are retired: the GCC 13
build against `jdominsk`'s libraries (2026-07-30) and the GCC 14 / CUDA 12.8.1
target against `hager`'s libraries. The 2026-08-19 system update moved Polaris
to CPE 26.03 with cray-mpich 9.1.0, and libraries built against cray-mpich 8.x
(`libmpi_gnu_123.so.12`, `libdsmml.so.0`) no longer load. Nothing under
`/grand/TokamakProfile/hager/` or `/grand/TokamakProfile/jdominsk/` is
referenced by the current build caches (checked 2026-09-09: zero matches in
both `CMakeCache.txt` files). Do not resurrect those paths; they fail at load
time, not at link time.

## Paths

| Item | Path |
|---|---|
| Source checkout | `/grand/TokamakProfile/shku/XGC-Devel` (remote `seunghoeku/XGC-Devel`; `master` at `8e863ba9`, 2026-09-09) |
| Production build dir | `/grand/TokamakProfile/shku/XGC-Devel/bin` (the directory is named `bin`; the executable is `bin/bin/xgc`) |
| Production executable | `/grand/TokamakProfile/shku/XGC-Devel/bin/bin/xgc` (740 MB, commit in `bin/git_commit.txt`) |
| Second build dir | `/grand/TokamakProfile/shku/XGC-Devel/build` (branch `polaris_lib_update`, commit `8db5c237`, smoke-tested job 7592997) |
| Environment script | `/grand/TokamakProfile/shku/XGC-Devel/quickstart/modules_and_env/polaris.sh` |
| Dependency install root | `/grand/TokamakProfile/shku/Software/install` |
| Dependency build scripts | `/grand/TokamakProfile/shku/Software/{env_polaris_gcc14.sh,build_deps.sh,build_and_test.pbs}`, logs in `Software/logs/` |

The filesystem name is `/grand`, not `/grad`.

Dependency stack under `Software/install` (all built with the modules below):

| Package | Directory |
|---|---|
| Kokkos 4.7.04 (Serial, OpenMP, CUDA, `AMPERE80`) | `kokkos/v4.7.04-gcc14-cuda13.0.1` |
| ADIOS2 2.12.2 (MPI, Fortran, no CUDA) | `adios/v2.12.2-gcc14` |
| PETSc 3.24.4 (fblaslapack, metis, parmetis) | `petsc/3.24.4-gcc14` |
| Ginkgo 1.11.0 (CUDA 80, OpenMP, MPI) | `ginkgo/ginkgo-1.11.0-gcc14-cuda13.0.1` |
| Python venv (numpy) | `cray-python-venv-3.12.12` |

## Which source tree to build

`master` is fine since 2026-09-09: PR #2799 (`polaris_lib_update`) merged
commit `d68083248`, which points `CMake/find_dependencies_polaris.cmake` at the
`shku/Software/install` stack and updates `quickstart/modules_and_env/polaris.sh`
to the module list below. Any checkout older than that (or a branch that has not
merged master since) still points at `hager`'s CPE 25.x / CUDA 12.8.1 stack and
will link against libraries that no longer load; check the first line of
`find_dependencies_polaris.cmake` before configuring.

Two quickstart files on `master` are still stale and must not be used as-is:
`quickstart/cmake_configs/polaris.sh` names `hager`'s `nvcc_wrapper` (use the
configure line below instead), and `quickstart/job_scripts/polaris.sh` carries
retired `filesystems=home:eagle` / `TokamakITER` settings from an older
allocation (the xgc-automation PBS template is the current job script).

## Environment

`quickstart/modules_and_env/polaris.sh` (same module set as
`Software/env_polaris_gcc14.sh`, which built the dependencies):

```bash
module use /soft/modulefiles
module swap PrgEnv-nvidia PrgEnv-gnu
module unload darshan
module load cray-fftw cray-libsci
module load cudatoolkit-standalone/13.0.1
module load spack-pe-base cmake
module load cray-python/3.12.12
module load googletest/1.14.0
module load ninja 2>/dev/null        # see below
export CUDA_ROOT=${CUDA_HOME}
export CRAYPE_LINK_TYPE=dynamic
export NVCC_WRAPPER_DEFAULT_COMPILER=CC
source /grand/TokamakProfile/shku/Software/install/cray-python-venv-3.12.12/bin/activate
export XGC_PLATFORM=polaris
```

Resulting toolchain: craype 2.7.36, gcc-native 14.3.0 behind `cc`/`CC`/`ftn`,
cray-mpich 9.1.0 (MPI 4.1), cray-libsci 26.03.0, cray-fftw 3.3.10.11, CUDA
13.0.1, CMake 3.30.5 (spack), Ninja 1.12.1.

Two traps in the module list:

- **`module unload darshan`.** `darshan/3.5.0` is auto-loaded and still
  linked against cray-mpich 8.x. Any executable built with the Cray wrappers
  while it is loaded pulls it in and fails at load time. The script unloads
  it; if you build from a hand-made environment, do the same.
- **`module load ninja` can fail** ("module ninja/1.12.1 is unknown"); the
  spack module is not always resolvable. The script falls back to the spack
  install prefix under `/soft/spack/pe/0.10.1/.../ninja-1.12.1-*/bin`. The
  same error appears in the stderr of every ssh command when a login shell
  rc tries the module; it is noise, not a failure.

## Configure and build (verified 2026-09-05)

```bash
source /grand/TokamakProfile/shku/XGC-Devel/quickstart/modules_and_env/polaris.sh
cd /grand/TokamakProfile/shku/XGC-Devel/build     # or a fresh directory
cmake -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DUSE_CABANA=Off \
  -DCMAKE_CXX_COMPILER=CC -DCMAKE_C_COMPILER=cc -DCMAKE_Fortran_COMPILER=ftn \
  -DCMAKE_CXX_STANDARD=17 -DCMAKE_Fortran_FLAGS="-Wno-argument-mismatch" ..
ninja -j 8 xgc                                    # 681 targets; -j 24 on a compute node
```

The C++ compiler is the Cray `CC` wrapper directly, not `nvcc_wrapper`. Kokkos
was built with `nvcc_wrapper` and `Kokkos_ARCH_AMPERE80`, and its CMake
package carries the CUDA settings into XGC; the configure log reports `Enabled
Kokkos devices: OPENMP;SERIAL;CUDA`. Configure takes about 30 s; the full
build took about five minutes at `-j 24` on a compute node (job 7592982).

Verified cache values (identical in `bin/` and `build/`):

```text
CMAKE_BUILD_TYPE=RelWithDebInfo
CMAKE_CXX_COMPILER=/opt/cray/pe/craype/2.7.36/bin/CC   (GNU 14.3.0)
CUDAToolkit_BIN_DIR=/soft/compilers/cudatoolkit/cuda-13.0.1/bin
Kokkos_DIR=.../shku/Software/install/kokkos/v4.7.04-gcc14-cuda13.0.1/lib64/cmake/Kokkos
ADIOS2_DIR=.../shku/Software/install/adios/v2.12.2-gcc14/lib64/cmake/adios2
PETSC_DIR=.../shku/Software/install/petsc/3.24.4-gcc14
Ginkgo_DIR=.../shku/Software/install/ginkgo/ginkgo-1.11.0-gcc14-cuda13.0.1/lib64/cmake/Ginkgo
USE_GPU=ON   USE_GPU_AWARE_MPI=OFF   USE_CABANA=Off   XGCA=OFF
```

HDF5, Omega_h, Xgc_Mesh_Reader, and FusionIO are reported NOTFOUND; all are
optional here and the build links without them.

## Verifying a new binary

1. `ldd bin/xgc | grep -E 'not found|darshan|_123'` prints nothing. `_123`
   matches the cray-mpich 8 library names; a hit means the environment was
   wrong when linking, most likely darshan still loaded.
2. `xgc --dryrun` cannot run on a login node: the CUDA build needs a visible
   GPU and login-node NVML is broken (`nvidia-smi` fails with "Failed to
   initialize NVML"; Kokkos aborts with `cudaErrorNoDevice` before XGC reads
   the input). Use a 1-node, 10-minute `debug` job; the dryrun itself takes
   10 to 20 s. `hpc_runner.submit` does this for you: on Polaris it submits
   the production job first and, if the job is still queued after 20 s, runs
   the dryrun as a parallel 1-node job (debug, or debug-scaling/preemptable
   when your debug slot is taken) whose verdict lands on the next status
   check (`docs/workflow.md`, "Dryrun on machines without a login-node
   dryrun").
3. Smoke test on 2 nodes x 4 A100 (8 ranks, `--ppn 4 --cpu-bind depth -d 16`,
   one GPU per rank via `CUDA_VISIBLE_DEVICES=$((3 - PMI_LOCAL_RANK % 4))`).
   `Software/build_and_test.pbs` is the job that did this with the 10-step
   ECBC case. The first attempt (7592982) had rank 3 exit with code 1; the
   rerun (7592997) passed and is what `machines.local.yaml` records as
   `runtime_tested`. The test directory `xgc_test_cuda13` has since been
   removed, so keep the job's stdout when you repeat this.
4. `[CRAYBLAS_WARNING] Application linked against multiple cray-libsci
   libraries` is printed at startup. Observed on the passing runs; harmless
   so far.

## Rebuilding the dependency stack

`Software/build_deps.sh [venv|kokkos|adios2|petsc|ginkgo|all]` sources
`env_polaris_gcc14.sh` and builds each package with `-j 8` into
`Software/install`, logging to `Software/logs/`. The cmake and configure
options mirror `hager`'s 2026-07 installation (see the header comment in the
script) but are compiled against cray-mpich 9.1.0. One trap: Ginkgo's
`csr.cpp` and `dense.cpp` get `cc1plus` killed on the login nodes (memory);
finish that build with `ninja install` on a compute node, which is step 1 of
`build_and_test.pbs`.

## GPU-aware MPI

The cray-mpich 9.1.0 GTL (`libmpi_gtl_cuda.so`) needs CUDA 13, which this
stack now provides, so GPU-aware MPI is possible for the first time on
Polaris. It is **off by default** to match the Perlmutter runs: the
environment script sets `PETSC_OPTIONS='-use_gpu_aware_mpi 0'` and leaves
`MPICH_GPU_SUPPORT_ENABLED` unset. To turn it on in a job script set
`MPICH_GPU_SUPPORT_ENABLED=1` and drop the PETSc option; treat that as an
experiment and compare against a run with it off. `machines.yaml` keeps
`gpu_aware_mpi: false`, which is still the right default, although the comment
there gives the old reason (the retired binary was not linked with the GTL and
forcing the variable aborted MPI initialisation).

## xgca (neoclassical)

There is currently **no `xgca` binary** on Polaris: `XGCA=OFF` in both caches,
and the July GCC 13 `xgca` disappeared with the rebuild. To build one, in an
existing configured build directory:

```bash
cmake -DXGCA=ON .
ninja -j 8 xgca        # took about 25 min on a login node in July
```

Record it as `xgc.xgca_exe` in `machines.local.yaml`. It is needed for the
NT/PT torque draft family (`kb/cases/drafts/`).

## PBS execution profile

`machines.yaml` and `templates/polaris.pbs.j2` record the submission profile.
A live hello-world round trip passed on 2026-07-30 (job 7301673,
`debug-scaling`, 1 node, 4 ranks), and production runs have used the template
since (`n648po` onward).

| Resource | Prototype default | Reason |
|---|---:|---|
| Queue | `debug-scaling` | The four-node prototype fits its 1–10 node range |
| Nodes | 4 | Small validation run |
| MPI ranks/node | 4 | One rank per A100 GPU |
| OpenMP threads/rank | 16 | Covers the 64 hardware threads on a node |
| GPUs/node | 4 | Full Polaris node |
| Filesystems | `home:grand` | PBS requires an explicit filesystem list |

The production queue is not the default because it requires at least 10
nodes. The template uses `mpiexec`, `place=scatter`, `system=polaris`, and
binds each local MPI rank to one GPU using `PMI_LOCAL_RANK`. It writes bounded
job logs as `pbs-${PBS_JOBID}.out` and `.err` inside the run directory.

The scheduler adapter validates `account`, `queue`, `filesystems`, and `system`
before rendering a PBS script. `qstat -x -f` terminal jobs are classified from
both `job_state` and `Exit_status`; a finished job with a nonzero exit status
is reported as failed.

Official references:

- [Running jobs on Polaris](https://docs.alcf.anl.gov/polaris/running-jobs/)
- [ALCF queue policy](https://docs.alcf.anl.gov/running-jobs/)
- [Polaris system overview](https://docs.alcf.anl.gov/polaris/)
