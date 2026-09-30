# Building XGC on the STELLAR-AI GPU partition (`XGC_PLATFORM=stellarai`)

Verified: 2026-09-29 / XGC-Devel `stellarai-gpu` @ `b3fa2c9b1`, built in
`/scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu/build` on `stellarai-vis2`
(alias `stellarai2`, plain ssh). Full notes: `docs/build-stellarai-gpu.md`.
CPU partition: `references/stellarai.md` (`XGC_PLATFORM=stellarai_cpu`).

## Rules

- Build on `stellarai-vis2`, not `stellarai-amd` (AMD host; the stack is
  compiled with `-march=sapphirerapids` for the Intel GPU nodes).
- vis2's own GPU is an RTX PRO 6000 (sm_120); the B200 is sm_100. Kokkos is
  pinned to `BLACKWELL100`. The binary does not run kernels on vis2, so there is
  no login-node dryrun. Dryrun inputs with the `stellarai_cpu` build.
- `/projects` is login-only: static dependencies, jobs source the env file from
  the scratch checkout, never from `/projects/XGC/STELLARAI/Software/bin`.
- Separate checkout (`XGC-Devel-gpu`) so CPU jobs keep their exe and env file.

## Build

```bash
source /scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu/quickstart/modules_and_env/stellarai.sh
cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu/build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 48 xgc     # ~9 min, 675 targets
ninja -j 48 xgca    # ~6 min, 247 targets
```

Do not pass compilers on the cmake line: the env file sets `CXX=mpicxx` and
`NVCC_WRAPPER_DEFAULT_COMPILER=mpicxx`, and the Kokkos package routes device
code through `nvcc_wrapper`.

## Traps already paid for

| Symptom | Fix |
|---|---|
| Ginkgo: `namespace "gko::thrust" has no member "make_pair"` | CUDA 13.2 CCCL dropped it; `build_deps.stellarai ginkgo` seds it to `thrust::make_tuple` |
| XGC link: `cannot find -lThrust` | static Ginkgo exports a `Thrust` target it does not define; the platform file adds an empty interface target |
| `mpif.h: No such file` in camtimers | OpenMPI 5 `mpifort -E` drops `-I`; platform file adds `-I$MPI_HOME/include` (load the modules in the cmake shell) |
| Ginkgo "compiler differs" CMake warning | g++ vs mpicxx, same gcc 14.3.1; harmless |

## Post-build checks

- `env -i ldd bin/xgc | grep -E 'not found|/projects'` prints nothing.
- `bin/xgc --info` (after sourcing the env file) shows `USING GPU VERSION`,
  `Kokkos backend = CUDA` and the expected commit with a clean tree.
- Record the build under `stellarai.xgc.observed_builds.gpu` in
  `machines.local.yaml`; the machine's default exe stays the CPU build.
