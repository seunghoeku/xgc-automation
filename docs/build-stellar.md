# Building XGC on stellar (Princeton)

Verified: 2026-07-28 / XGC-Devel `master` @ `256c3098`

## Summary

```bash
source /projects/XGC/STELLAR/Software/bin/set_up_xgc.stellar

cd ~/git/XGC-Devel
rm -rf build && mkdir build && cd build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 16 xgc          # produces: build/bin/xgc
```

Two things you must get right. The build breaks otherwise:

1. **Always source the environment script.** It sets the compilers
   (`CC`/`CXX`/`FC`), `XGC_PLATFORM=stellar`, and `FFTW3DIR`/`HDF5DIR`.
2. **Use `CMAKE_BUILD_TYPE=Release`.** The default and `RelWithDebInfo` both fail
   with a compiler crash (see below).

## Environment

What `/projects/XGC/STELLAR/Software/bin/set_up_xgc.stellar` provides:

| Item | Value |
|---|---|
| Compiler | Intel oneAPI 2024.2 (`icpx`/`ifx`) |
| MPI | OpenMPI 4.1.6 (`mpicc`/`mpicxx`/`mpif90`) |
| CMake | 3.24.2 (`cmake/pppl/3.24.2`) |
| Dependencies | `/projects/XGC/STELLAR/Software/install/` (petsc 3.24.4, adios2, kokkos, cabana, pspline, camtimers, gtest) |
| Platform | `XGC_PLATFORM=stellar` → `CMake/find_dependencies_stellar.cmake` |

Do not set compilers manually via `cmake -DCMAKE_CXX_COMPILER=...`.
`find_dependencies_stellar.cmake` reads `$ENV{FFTW3DIR}` and `$ENV{HDF5DIR}`, so
dependency discovery fails without sourcing the script.

## Pitfall 1 — Cray wrappers (`ftn`/`CC`/`cc`)

Reusing a Perlmutter/Polaris configure command gives:

```
Compiling the Fortran compiler identification source file ... failed.
Compiler: ftn
The output was: No such file or directory
```

`ftn` and `CC` belong to the Cray programming environment and do not exist on
stellar. `cc` is worse: it resolves to `/usr/bin/cc` (system GCC), so the build
silently proceeds with the wrong compiler instead of failing.

Once a wrong compiler is written into `CMakeCache.txt`, fixing the environment is
not enough — the cached value wins. **Delete the build directory and re-run
configure.**

## Pitfall 2 — icpx crashes at `-O2` (the important one)

Building with `RelWithDebInfo` (= `-O2 -g -DNDEBUG`) gives:

```
icpx: error: clang frontend command failed with exit code 139
```

Exit 139 = SIGSEGV. **This is not a source bug — the compiler itself dies.**
The stack dump identifies the culprit:

```
llvm::vpo::VPlanCostModelWithHeuristics<...>::getLoadStoreCost(...)
llvm::vpo::VPlanPeelingAnalysis::selectBestStaticPeelCount(...)
llvm::vpo::LoopVectorizationPlanner::selectBestPlan()
```

The icpx loop vectorizer (VPlan) crashes while computing loop peeling costs. It
appears to trigger on XGC's `FORCE_SIMD` (= `#pragma omp simd`, defined in
`XGC_core/cpp/utils/simd.hpp:11`) loops processed with `SIMD_SIZE=32`.

This is not confined to one file. Confirmed failures so far:
`XGC_core/cpp/grid/calc_mag_drift_flux_avg.cpp` and
`XGC_core/cpp/sources/current_drive.cpp` — working around one just moves the
crash to the next file.

### Measured results per flag (identical for both files)

| Flags | Result |
|---|---|
| `-O2 -g -DNDEBUG` (RelWithDebInfo) | **FAIL** |
| `-O2 -DNDEBUG` | **FAIL** |
| `-O3 -DNDEBUG` (Release) | OK |
| `-O3 -g -DNDEBUG` | OK |
| `-O0` / `-O1` | OK |
| `-O2 -fno-vectorize` | OK |
| `-O2 -mno-avx512f` | FAIL |
| `-O2 -fno-slp-vectorize` | FAIL |
| `-O2 -fp-model=precise` | FAIL |

`-g` is irrelevant. **Only `-O2` crashes; `-O3` is fine.** Counterintuitive, but
`-O3` uses different vectorization heuristics and cost-model paths than `-O2`, so
it never reaches the crashing code path. AVX-512 and SLP vectorization are not
involved — the loop vectorizer alone is responsible.

### Why Release rather than `-fno-vectorize`

Both complete the build, but performance differs:

| | `-O2 -fno-vectorize` | **Release (`-O3`)** |
|---|---|---|
| Build | 248/248 OK | 248/248 OK |
| `loop not vectorized` warnings | 395 | **89** |
| Binary size | 39.3 MB | 40.1 MB |
| Vectorization | fully disabled | working |

`-fno-vectorize` disables SIMD entirely. XGC's particle push depends on
vectorization for performance, so it is unsuitable for production. Release keeps
vectorization and merely avoids the crash.

### If you need a debug build

`RelWithDebInfo` is unusable with this compiler. Use instead:

```bash
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-g ..
```

The `-O3 -g` combination is verified working in the table above.

### Upgrading the compiler is not currently an option

stellar provides only `intel-oneapi/2024.2` (confirmed via `module avail`), so
Release is effectively the only fix.

This crash is worth reporting to Intel. icpx already writes reproducer files:
`/tmp/icpx-*/calc_mag_drift_flux_avg-*.cpp` and the matching `.sh`.

## Open issues / observed warnings

Configure and build both succeed, but these are worth keeping in mind:

- `HDF5 Fortran compiler wrapper is unable to compile a minimal HDF5 program`
  — CMake falls back to naming the libraries directly and proceeds.
- `Performing Test FILESYSTEM_LINKS_WITHOUT_LIB - Failed`
  — `std::filesystem` needs a separate library. Linking currently succeeds.
- Ginkgo and FusionIO not found — optional dependencies, harmless.
- Kokkos is installed without GPU support → `USE_OMP;USE_SERIAL` CPU backends.
  The stellar build is CPU-only (`XGC_TYPES=cpu`).

## Scope of verification

`ninja -j 16 xgc` completed 248/248 targets with zero crashes and produced
`bin/xgc` (40 MB ELF). **Runtime execution has not been verified yet.**
