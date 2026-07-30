# Building XGC on Polaris

Status: CMake configure verified on 2026-07-29. A read-only remote check found
only CMake compiler-probe binaries in `build`; the XGC executable has not been
compiled or validated.

## Paths

| Item | Path |
|---|---|
| Source | `/grand/TokamakProfile/shku/XGC-Devel` |
| Build | `/grand/TokamakProfile/shku/XGC-Devel/build` |
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

CMake enabled the OpenMP, Serial, and CUDA Kokkos backends and generated Ninja
build files successfully. No `/eagle/TokamakITER` references remain in the
`CMake` sources, Polaris environment script, `CMakeCache.txt`, or
`build.ninja`. The common typo `/grad/TokamakProfile` is also absent.

Warnings about Cabana, Ginkgo, Omega_h, Xgc_Mesh_Reader, HDF5, and FusionIO
were non-blocking for this configuration. The Cabana warning is expected:
with `USE_CABANA=OFF`, XGC probes for Cabana without requiring it.

## PBS execution profile

`machines.yaml` and `templates/polaris.pbs.j2` now record the local submission
profile, but no live `qsub` has been run through the automation yet.

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

Before an XGC submission can be planned without an executable override:

1. Compile successfully in `/grand/TokamakProfile/shku/XGC-Devel/build`.
2. Record the resulting executable in `machines.yaml` under `polaris.xgc.exe`.
3. Open the Polaris MFA/SSH session manually if its ControlMaster has expired.
4. Preview a small hello-world plan and obtain human approval before `qsub`.
5. Verify the `qstat` and output paths, then run an XGC dryrun before any
   allocation-consuming scientific job.
