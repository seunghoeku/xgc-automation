# Building XGC on Polaris

Status: configure and the `xgc` target compiled successfully on 2026-07-30.
Runtime execution on a compute node has not yet been tested.

## Paths

| Item | Path |
|---|---|
| Source | `/grand/TokamakProfile/shku/XGC-Devel` |
| Build | `/grand/TokamakProfile/shku/XGC-Devel/build` |
| Executable | `/grand/TokamakProfile/shku/XGC-Devel/build/bin/xgc` |
| Shared Kokkos modules | `/grand/TokamakProfile/Software/polaris/modulefiles` |
| PETSc and ADIOS2 | `/grand/TokamakProfile/jdominsk/libraries/install` |
| CUDA toolkit | `/soft/compilers/cudatoolkit/cuda-12.6.3` |

The filesystem name is `/grand`, not `/grad`.

## Environment

`quickstart/modules_and_env/polaris.sh` must use the migrated module tree:

```bash
module use /grand/TokamakProfile/Software/polaris/modulefiles
module load kokkos/4.6.02_shared_PEgnu8.6.0_cv13.2_ct12.6.3
```

Cabana is not installed in the migrated tree. Do not load its old module;
configure XGC with `USE_CABANA=OFF`.

The `xgc_cmake` interactive-shell alias expands to:

```bash
cmake -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_CXX_COMPILER=CC \
  -DCMAKE_C_COMPILER=cc \
  -DCMAKE_Fortran_COMPILER=ftn \
  -DCMAKE_CXX_STANDARD=17 \
  -DCMAKE_Fortran_FLAGS="-Wno-argument-mismatch"
```

## Verified configure

After sourcing `quickstart/modules_and_env/polaris.sh`, configure in the empty
build directory with the alias options plus:

```bash
xgc_cmake \
  -DUSE_CABANA=OFF \
  -DCUDAToolkit_ROOT=/soft/compilers/cudatoolkit/cuda-12.6.3 \
  ..
```

The explicit CUDA root is important. An earlier configure selected the system
CUDA 12.9.1 even though the migrated Kokkos installation was built against
CUDA 12.6.3.

The verified cache contains:

```text
ADIOS2_DIR=/grand/TokamakProfile/jdominsk/libraries/install/ADIOS2-2.10.2/lib64/cmake/adios2
CMAKE_BUILD_TYPE=RelWithDebInfo
CUDAToolkit_BIN_DIR=/soft/compilers/cudatoolkit/cuda-12.6.3/bin
PETSC_DIR=/grand/TokamakProfile/jdominsk/libraries/install/petsc-3.24.1
USE_CABANA=OFF
```

## Verified compilation

The `xgc` target was built on the login node with bounded parallelism:

```bash
ninja -C /grand/TokamakProfile/shku/XGC-Devel/build -j 8 xgc
```

Ninja reported a 678-edge build and successfully linked `build/bin/xgc`.
Verification:

| Check | Result |
|---|---|
| Source commit | `60bef3bcef9fdab9709cab43d52a28dd89d8cb6f` |
| Executable size | 669,944,456 bytes |
| Executable permission | `-rwxr-xr-x` |
| PrgEnv | `PrgEnv-gnu/8.6.0` |
| `cc`, `CC`, `ftn` backend | GNU 13.3.1 |
| Dynamic dependencies | All resolved; no `not found` entries |
| PETSc | `/grand/TokamakProfile/jdominsk/libraries/install/petsc-3.24.1` |
| ADIOS2 | `/grand/TokamakProfile/jdominsk/libraries/install/ADIOS2-2.10.2` |
| Kokkos | `/grand/TokamakProfile/Software/polaris/kokkos/.../gnu/13.2/...` |
| CUDA runtime | `/soft/compilers/cudatoolkit/cuda-12.6.3` |

The module list also displays `gcc-native/14.2`, but the Cray compiler wrappers
actually resolve to GNU 13.3.1. This is the compiler identity relevant to the
XGC objects and matches the GNU 13 family used for the shared Kokkos build.

CMake enabled the OpenMP, Serial, and CUDA Kokkos backends and generated Ninja
build files successfully. No `/eagle/TokamakITER` references remain in the
`CMake` sources, Polaris environment script, `CMakeCache.txt`, or
`build.ninja`. The common typo `/grad/TokamakProfile` is also absent.

Warnings about Cabana, Ginkgo, Omega_h, Xgc_Mesh_Reader, HDF5, and FusionIO
were non-blocking for this configuration. The Cabana warning is expected:
with `USE_CABANA=OFF`, XGC probes for Cabana without requiring it.

## PBS execution profile

`machines.yaml` and `templates/polaris.pbs.j2` record the submission profile.
A live hello-world round trip passed on 2026-07-30:

| Check | Result |
|---|---|
| Job ID | `7301673.polaris-pbs-01.hsn.cm.polaris.alcf.anl.gov` |
| Queue/resources | `debug-scaling`, 1 node, 4 ranks, 10-minute limit |
| Compute host | `x3005c0s7b0n0` |
| Launcher | `mpiexec --np 4 -ppn 4 hostname` |
| Scheduler result | `completed`, `Exit_status=0` |
| Output | Four hostname lines and `rc=0` |

The run is recorded locally under `runs/pbs-smoke-20260730/`. Live
`qsub`, extended `qstat`, terminal exit-code parsing, and bounded output reads
are therefore verified. The cancellation command remains unit-tested but has
not been exercised against a live queued job.

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

## Remaining verification

Before the first XGC allocation:

1. Open the Polaris MFA/SSH session manually if its ControlMaster has expired.
2. Stage a validated XGC case and run the login-node dryrun.
3. Preview the XGC job plan and obtain human approval before `qsub`.
