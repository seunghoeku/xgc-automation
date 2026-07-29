# Building XGC on Polaris

Status: CMake configure verified on 2026-07-29. Compilation has not yet been
run or validated.

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
generated build tree.

Warnings about Cabana, Ginkgo, Omega_h, Xgc_Mesh_Reader, HDF5, and FusionIO
were non-blocking for this configuration. The Cabana warning is expected:
with `USE_CABANA=OFF`, XGC probes for Cabana without requiring it.
