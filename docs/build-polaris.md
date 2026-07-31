# Building XGC on Polaris

Status: the GCC 13 `xgc` target compiled successfully on 2026-07-30, but its
compute-node runtime remains unverified. XGC-Devel upstream `master` at
`f805e622` now contains a new GCC 14/CUDA 12.8.1 setup. That target setup is
documented below but has not yet been built or runtime-tested in the `shku`
checkout.

Update 2026-07-31: `xgc --dryrun` works on compute nodes (debug queue) and has
validated three promoted cases (jobs 7307649, 7307755, 7307766). Login-node
dryrun is NOT possible: the CUDA build requires a visible GPU and the login
nodes' NVML is broken (`nvidia-smi` fails with "Failed to initialize NVML"
even though an A100 and /dev/nvidia0 are present). Kokkos aborts with
`cudaErrorNoDevice` before XGC reads the input. Use a 1-node/10-min debug-queue
job instead; the dryrun itself takes ~10-20 s.

## xgca (neoclassical) target

Built 2026-07-31 in the same GCC 13 build directory (commit `f805e6223`):

```bash
cd /grand/TokamakProfile/shku/XGC-Devel/build
source ../quickstart/modules_and_env/polaris.sh
cmake -DXGCA=ON .          # cache previously had XGCA:BOOL=OFF
ninja -j 8 xgca            # 677 edges, ~25 min on the login node
```

Result: `build/bin/xgca` (671 MB), recorded as `xgc.xgca_exe` in
`machines.yaml`. Status: compiled only. No dryrun or compute-node smoke test
has been run. It is needed to validate the NT/PT torque draft family
(kb/cases/drafts/, notebook outcome "Used for Paper"). hager also has xgca
binaries under `/grand/TokamakProfile/hager/XGC-Devel/_build3/bin/` but with
unrecorded commit/environment; prefer the shku build for provenance.

## Paths

| Item | Path |
|---|---|
| Source | `/grand/TokamakProfile/shku/XGC-Devel` |
| Verified GCC 13 build | `/grand/TokamakProfile/shku/XGC-Devel/build` |
| Verified GCC 13 executable | `/grand/TokamakProfile/shku/XGC-Devel/build/bin/xgc` |
| GCC 14 target source/worktree | `/grand/TokamakProfile/shku/XGC-Devel-gcc14` |
| GCC 14 target build | `/grand/TokamakProfile/shku/XGC-Devel-gcc14/build` |
| GCC 14 dependency root | `/grand/TokamakProfile/hager/Software/install` |
| GCC 14 CUDA toolkit | `cudatoolkit-standalone/12.8.1` |

The filesystem name is `/grand`, not `/grad`.

## GCC 14 master environment

Commit `7c3d278f` replaces the previous project Kokkos/Cabana modules with a
self-contained GCC 14 dependency installation. The master environment does:

```bash
module reset
module use /soft/modulefiles
module swap PrgEnv-nvidia PrgEnv-gnu
module load cray-fftw cray-libsci
module load cudatoolkit-standalone/12.8.1
module load spack-pe-base cmake
module load cray-python/3.11.7
module load googletest/1.14.0

export CUDA_ROOT=${CUDA_HOME}
export CRAYPE_LINK_TYPE=dynamic
export NVCC_WRAPPER_DEFAULT_COMPILER=CC
source /grand/TokamakProfile/hager/Software/install/cray-python-venv-3.11.7/bin/activate
```

The corresponding dependency roots in `CMake/find_dependencies_polaris.cmake`
are:

| Package | GCC 14 master path |
|---|---|
| ADIOS2 | `/grand/TokamakProfile/hager/Software/install/adios/v2.12.2-gcc14` |
| Kokkos | `/grand/TokamakProfile/hager/Software/install/kokkos/v4.7.04-gcc14-cuda12.8.1` |
| Ginkgo | `/grand/TokamakProfile/hager/Software/install/ginkgo/ginkgo-1.11.0-gcc14-cuda12.8.1` |
| PETSc | `/grand/TokamakProfile/hager/Software/install/petsc/3.24.4-gcc14` |

Cabana remains disabled. Do not carry the previous jdominsk GCC 13 dependency
paths into this build.

## GCC 14 target configure

Preserve the verified GCC 13 checkout because `machines.yaml` sources its
runtime environment script. Create a separate master worktree at
`/grand/TokamakProfile/shku/XGC-Devel-gcc14`, then configure its empty `build`
directory after sourcing that worktree's environment script:

```bash
cmake \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_CXX_COMPILER=/grand/TokamakProfile/hager/Software/install/kokkos/v4.7.04-gcc14-cuda12.8.1/bin/nvcc_wrapper \
  -DCMAKE_C_COMPILER=cc \
  -DCMAKE_Fortran_COMPILER=ftn \
  -DCMAKE_CXX_STANDARD=17 \
  -DCMAKE_Fortran_FLAGS="-Wno-argument-mismatch" \
  -DUSE_CABANA=OFF \
  ..
```

Build using the generator-independent interface:

```bash
cmake --build /grand/TokamakProfile/shku/XGC-Devel-gcc14/build \
  --target xgc --parallel 8
```

Before changing `machines.yaml`, verify the compiler identity, dynamic
libraries, absence of retired `/eagle` paths, login-node dryrun, and a short
compute-node run. Then move `xgc.src`, `xgc.build`, `xgc.exe`, and
`xgc.env_script` together to the GCC 14 worktree and record its commit. Updating
only the source checkout would mix the old executable with the new environment.

## Previous verified GCC 13 configure

The 2026-07-30 build used the previous environment and remains available as a
rollback reference:

```bash
xgc_cmake \
  -DUSE_CABANA=OFF \
  -DCUDAToolkit_ROOT=/soft/compilers/cudatoolkit/cuda-12.6.3 \
  ..
```

The explicit CUDA root was required for that older Kokkos installation. It is
not the configuration used by GCC 14 master.

The verified cache contains:

```text
ADIOS2_DIR=/grand/TokamakProfile/jdominsk/libraries/install/ADIOS2-2.10.2/lib64/cmake/adios2
CMAKE_BUILD_TYPE=RelWithDebInfo
CUDAToolkit_BIN_DIR=/soft/compilers/cudatoolkit/cuda-12.6.3/bin
PETSC_DIR=/grand/TokamakProfile/jdominsk/libraries/install/petsc-3.24.1
USE_CABANA=OFF
```

## Previous verified GCC 13 compilation

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

These compiler and library identities describe only the rollback GCC 13
binary, not the new master target.

This executable is not linked with the Cray GPU Transport Layer (GTL).
Accordingly, `machines.yaml` sets `xgc.gpu_aware_mpi: false`, matching the
commented-out `MPICH_GPU_SUPPORT_ENABLED` line in XGC's Polaris environment
script. Forcing that variable to `1` makes Cray MPICH abort during
initialization. GPU rank affinity remains enabled independently through
`CUDA_VISIBLE_DEVICES`.

CMake enabled the OpenMP, Serial, and CUDA Kokkos backends and generated Ninja
build files successfully. No `/eagle/TokamakITER` references remain in that
legacy build cache. The common typo `/grad/TokamakProfile` is also absent.

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
