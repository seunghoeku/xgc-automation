---
name: xgc-build
description: Build XGC (gyrokinetic code, XGC-Devel) on an HPC machine - stellar (Princeton), Polaris (ALCF), Aurora (ALCF) - with the per-machine environment script, cmake configure, compiler traps, and post-build checks that have already cost whole sessions to rediscover. Use this whenever the user wants to compile, rebuild, reconfigure, or debug a build of xgc or xgca on any of these machines, hits a cmake/ninja/icpx/nvcc_wrapper error while building XGC, asks which CMAKE_BUILD_TYPE or compiler to use, wants a debug build, needs to run xgc --dryrun for the first time after a build, or asks where a verified XGC executable lives on one of these machines - even if they do not say "build" and just paste a compiler error.
---

# Building XGC

XGC builds are fragile in machine-specific ways: the wrong compiler wrapper
gets silently cached, one optimisation level crashes the compiler, a GPU
build refuses to dry-run on a login node. Each machine has its own reference
file; read the one that matches before running anything, then follow it
rather than a configure line remembered from another machine.

| Machine | Reference | Toolchain | Status |
|---|---|---|---|
| stellar (Princeton) | [references/stellar.md](references/stellar.md) | Intel oneAPI 2024.2, CPU only | Build verified 2026-07-28; runtime not yet |
| STELLAR-AI (Princeton/PPPL) | [references/stellarai.md](references/stellarai.md) | gcc 14.3.1 + OpenMPI 5.0.10, AMD Zen 5, CPU only, `XGC_PLATFORM=stellarai_cpu` | Built 2026-09-22 with static deps; smoke test d0008sa passed (job 283). /projects is login-only; srun needs `-c --cpu-bind=cores` |
| STELLAR-AI GPU partition | [references/stellarai-gpu.md](references/stellarai-gpu.md) | gcc 14.3.1 + CUDA 13.2 + CUDA OpenMPI 5.0.10, 8x B200 (sm_100), `XGC_PLATFORM=stellarai` | xgc + xgca built 2026-09-29 on stellarai-vis2 with static deps incl. Ginkgo; login-node GPU is sm_120, so no login dryrun of the GPU binary |
| Polaris (ALCF) | [references/polaris.md](references/polaris.md) | CPE 26.03: gcc 14.3 + CUDA 13.0.1 (Cray PE) | Production binary built 2026-09-05, smoke-tested on 2 nodes; older GCC 13 / CUDA 12.8 stacks retired after the 2026-08-19 system update |
| Aurora (ALCF) | [references/aurora.md](references/aurora.md) | oneAPI IntelLLVM 2025.3.2 + SYCL, PE 26.26.0 | User build ran a 2-node smoke test 2026-09-09; rebuild after every PE update; set CMAKE_BUILD_TYPE explicitly |

If the machine is not one of these, start from XGC-Devel's own
`quickstart/{modules_and_env,cmake_configs}/<machine>.sh` and the official
build page (https://xgc.pppl.gov/html/cmake_build_instructions.html), and
write down what worked so it can become a reference file.

## Rules that hold on every machine

1. **Source the machine's environment script first, in the same shell that
   runs cmake.** It sets the compilers, `XGC_PLATFORM`, and the dependency
   roots that `CMake/find_dependencies_<machine>.cmake` reads from the
   environment. Configuring without it fails late or, worse, finds the wrong
   compiler and continues.
2. **Never reuse a configure line from another machine.** Cray wrappers
   (`cc`/`CC`/`ftn`) exist on Polaris and Aurora, not on stellar; oneAPI
   wrappers exist on stellar and Aurora, not on Polaris. A wrong compiler
   written into `CMakeCache.txt` wins over any later environment fix, so when
   the compiler identity is wrong, delete the build directory and configure
   again instead of patching the cache.
3. **Bound login-node parallelism.** `-j 8` to `-j 16` is what the references
   used; login nodes are shared and a full-width build gets killed or slows
   everyone. A full `xgc` build is roughly 250 targets on stellar and about
   680 on Polaris (25 minutes for `xgca` at `-j 8`).
4. **Prefer building from a clean `build/` directory** when changing
   compilers, build type, or dependency roots. Incremental rebuilds are fine
   for source edits.
5. **Do not run commands the user has not seen when they cost allocation.**
   Compiling on a login node is free; a compute-node dryrun or smoke test
   is a scheduler job. Show the job request (queue, nodes, wall time) and
   get approval before submitting it.

## After the build

A linked binary is not a verified build. Before pointing any run
configuration at it, check and record:

- **Compiler identity and build type** from `CMakeCache.txt`
  (`CMAKE_CXX_COMPILER`, `CMAKE_BUILD_TYPE`), and that no retired paths
  leaked in (Polaris: `/eagle/...`, `hager`/`jdominsk` library roots, the typo `/grad/`).
- **Dynamic libraries resolve**: `ldd bin/xgc | grep 'not found'` prints
  nothing.
- **Source commit**: `git -C <src> rev-parse HEAD`, plus the modules and
  dependency roots that were loaded. A binary whose provenance is unknown
  cannot be trusted for a paper, even if it runs.
- **`xgc --dryrun`** on a real input. On stellar (CPU) this works on the
  login node. On Polaris the CUDA build needs a visible GPU and login-node
  NVML is broken, so use a 1-node, 10-minute debug job. On Aurora the login
  nodes have no PALS launcher context, so `MPI_Init` dies before the input is
  read; use a 1-node `debug` job there too.

When working inside the `xgc-automation` repository, record `xgc.src`,
`xgc.build`, `xgc.exe`, and `xgc.env_script` together (personal paths in
`machines.local.yaml`, universal facts in `machines.yaml`). Moving only one
of them mixes an old executable with a new environment.

## Diagnosing a failed build

- **"Compiling the Fortran compiler identification source file ... failed"
  with `ftn`**: Cray wrapper on a non-Cray machine. Stellar; see rule 2.
- **`icpx: error: clang frontend command failed with exit code 139`**: the
  Intel compiler itself segfaulted, not a source bug. On stellar this is the
  `-O2` vectorizer crash; use `CMAKE_BUILD_TYPE=Release` (`-O3`). Details
  and the flag table are in the stellar reference.
- **`cudaErrorNoDevice` / "Failed to initialize NVML" (Polaris) or "Fatal
  error in internal_Init: Other MPI error" (Aurora) during dryrun**: you are
  on a login node with a GPU build. Not a build problem; run the dryrun in a
  1-node debug-queue job.
- **17 unresolved libraries on a binary that used to run (Aurora)**: the
  programming environment moved under it. Rebuild after every PE update.
- **Binary fails to load with `libmpi_gnu_123.so.12` or `libdsmml.so.0`
  missing (Polaris)**: it was linked against the pre-2026-08-19 cray-mpich 8
  stack, or `darshan` was still loaded at link time. Rebuild with the current
  environment script; see the Polaris reference.
- **GPU-aware MPI**: off by default on Polaris (`PETSC_OPTIONS='-use_gpu_aware_mpi 0'`);
  the CPE 26.03 stack can enable it, but treat that as an experiment. Aurora's
  config expects `-DUSE_GPU_AWARE_MPI=On` and `MPIR_CVAR_ENABLE_GPU=1`.
- **Warnings about Cabana, Ginkgo, Omega_h, FusionIO, HDF5 Fortran wrapper,
  `FILESYSTEM_LINKS_WITHOUT_LIB`**: optional or non-blocking on every machine
  so far. Do not chase them unless the link step fails.

## Reporting back

Summarise for the user: machine, source commit, build type, compiler, the
executable path and size, which of the post-build checks passed, and what
remains unverified (runtime, dryrun, smoke test). If a step was skipped or
failed, say so plainly; the reference files list what "verified" meant on
the date they were written, and a new build should be held to the same bar.
