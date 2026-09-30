# Building XGC on the STELLAR-AI GPU partition (`XGC_PLATFORM=stellarai`)

Status 2026-09-29: dependency stack installed. `xgc` and `xgca` built from
XGC-Devel branch `stellarai-gpu` @ `b3fa2c9b1` with `XGC_PLATFORM=stellarai`;
`--info` reports the GPU version with the CUDA Kokkos backend. First
compute-node run: d0030sa (job 332, 1 node) runs with one GPU per rank.

STELLAR-AI has two partitions under one Slurm (26.05) and one `/scratch/gpfs`.
The CPU partition and its build are in `docs/build-stellarai.md`
(`XGC_PLATFORM=stellarai_cpu`). The naming follows Perlmutter: the GPU platform
is `stellarai`, the CPU platform `stellarai_cpu`.

| | GPU partition |
|---|---|
| Login node for building | `stellarai-vis2.princeton.edu` (ssh alias `stellarai2`, plain ssh key login, no Duo): 2x Intel Xeon 6747P, 96 cores, 1 TB, one RTX PRO 6000 Blackwell (sm_120) |
| Compute nodes | 10 (`sai-n01g[1-7]`, `sai-n02g[1-6]`, `sai-n03g[1-7]`), features `intel,gpu`: 8x NVIDIA B200 (sm_100), 96 cores (2 sockets, no SMT), 980 GB |
| Partition, limit | `gpu`, 7 days; account `ptheory` |
| Driver on vis2 | 615.71.09 (CUDA 13.x) |

The GPU nodes were `DOWN+NOT_RESPONDING` ("Node unexpectedly rebooted") for
most of 2026-09-29; 8 of them were idle again by the evening. Both login nodes
see the same Slurm, so jobs for `-p gpu` can be submitted from
`stellarai-amd` as well.

**The login-node GPU is not the compute GPU.** vis2's RTX PRO 6000 is sm_120,
the B200 is sm_100. Kokkos is therefore built with `Kokkos_ARCH_BLACKWELL100`
set explicitly, and the resulting `xgc` cannot run its kernels on vis2. Do
not expect a login-node dryrun of the GPU binary to work (the `stellarai_cpu`
binary can still dryrun the input on the CPU login node).

## Filesystems

Same rule as the CPU partition: `/projects` is on the login nodes only
(assumed for the GPU nodes as well; not yet checked on one), so
every dependency is **static**, and jobs source the env file from the scratch
checkout. The GPU checkout is separate from the CPU one, so a GPU rebuild never
touches the executable or env file that CPU jobs use.

## Paths

| Item | Path |
|---|---|
| Env file (jobs and builds) | `<checkout>/quickstart/modules_and_env/stellarai.sh` |
| Installed copy of the env file | `/projects/XGC/STELLARAI/Software/bin/set_up_xgc.stellarai` (login nodes only; keep identical) |
| Dependency build script | `/projects/XGC/STELLARAI/Software/bin/build_deps.stellarai [ninja\|fftw\|kokkos\|adios2\|gtest\|petsc\|ginkgo\|all]` |
| Dependency install | `/projects/XGC/STELLARAI/Software/install/<pkg>/<version>/gcc-14.3-cuda-13.2-openmpi-5.0.10` |
| Logs | `/projects/XGC/STELLARAI/Software/src/logs/gcc-14.3-cuda-13.2-openmpi-5.0.10/`, summary `src/logs_build_deps_gpu.out` |
| XGC checkout | `/scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu`, branch `stellarai-gpu` |
| Build dir | `/scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu/build` |
| Platform file | `CMake/find_dependencies_stellarai.cmake`; quickstart `quickstart/{modules_and_env,cmake_configs,job_scripts,runlines}/stellarai.sh`; section "STELLAR-AI (GPU)" in `Docs/cmake_build_instructions.rst` |

The CPU files were renamed on 2026-09-29 to match:
`bin/set_up_xgc.stellarai_cpu` and `bin/build_deps.stellarai_cpu`. The CPU
install directories (`<version>/gcc-14.3-openmpi-5.0.10`) and the
`<version>/DEFAULT`, `<pkg>/DEFAULT` links are unchanged and still belong to the
CPU stack. The GPU script never touches the `DEFAULT` links, and the GPU
platform file names `<version>/<tag>` explicitly, as Perlmutter's does.

Branch sync (the login node has no GitHub key), as for the CPU checkout:

```bash
git push -f stellarai2:/scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu stellarai-gpu:refs/heads/sync_tmp
```

```bash
ssh stellarai2 'cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu && git reset --hard sync_tmp && git branch -D sync_tmp'
```

## Toolchain and dependency stack

Site modules, loaded by the env file: `gcc/14` (14.3.1), `cudatoolkit/13.2`
(nvcc 13.2.78), `openmpi/cuda-13.2/gcc/5.0.10` (built `--with-cuda`, UCX
1.22, pulls in `hpcx-latest/hpcx-stack`). No HDF5 module: HDF5 is only needed
by FusionIO, which is not installed, and the site HDF5 is built against the
non-CUDA OpenMPI.

Built from source, all static; host code with `-O3 -march=sapphirerapids`
(a subset of the Xeon 6 ISA; `znver5`, used by the CPU stack, would not be safe
on Intel):

| Package | Options |
|---|---|
| ninja 1.13.1 | build tool only |
| FFTW 3.3.10 | static, OpenMP, threads, MPI, AVX2, AVX-512; own source copy `src/fftw-3.3.10-<tag>` because FFTW builds in-tree |
| Kokkos 4.7.04 | Serial + OpenMP + CUDA, `Kokkos_ARCH_SPR`, `Kokkos_ARCH_BLACKWELL100`, CUDA lambda and constexpr, built with Kokkos's `nvcc_wrapper` |
| ADIOS2 2.12.1 | static, MPI, Fortran, SST; no CUDA, Kokkos, HDF5, Python, ZeroMQ, DataMan, MGARD |
| gtest 1.17.0 | with gmock |
| PETSc 3.24.4 | host only (`--with-cuda=0`), static, fblaslapack, metis, parmetis, zlib, OpenMP, threadsafety, Fortran bindings; `PETSC_ARCH=arch-<tag>` in the shared source tree |
| Ginkgo 1.11.0 | static, reference + OpenMP + CUDA (arch 100); no MPI, HIP, SYCL, tests, examples, benchmarks |

Sources in `src/` are shared with the CPU stack; build trees are
`_build_<tag>`. The env file sets `NVCC_WRAPPER_DEFAULT_COMPILER=mpicxx` and
`CXX=mpicxx`; Kokkos's CMake package sends device code through
`nvcc_wrapper`, which calls `mpicxx` as the host compiler (the Polaris
arrangement).

A full `all` run takes about 50 minutes on vis2 at `J=32`: FFTW 4, Kokkos 1,
ADIOS2 4, PETSc 11 and Ginkgo 27 minutes.

### Pitfall: Ginkgo 1.11.0 and CUDA 13.2 (`thrust::make_pair`)

The CCCL shipped with CUDA 13.2 no longer provides `thrust::make_pair`, so
`csr_kernels`, `fbcsr_kernels` and `rcm_kernels` fail with `namespace
"gko::thrust" has no member "make_pair"` (Polaris, on CUDA 13.0.1, did not hit
this). `build_ginkgo` rewrites the three calls to `thrust::make_tuple` before
configuring. Tuples compare lexicographically too, so the sort order is
unchanged.

## Build

```bash
source /scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu/quickstart/modules_and_env/stellarai.sh
cd /scratch/gpfs/PTHEORY/seungku/XGC-Devel-gpu
rm -rf build && mkdir build && cd build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 48 xgc
ninja -j 48 xgca
```

`ninja xgc` takes about 9 minutes at `-j 48` (675 targets; `bin/xgc` 393 MB),
and `ninja xgca` about 6 more (247 targets; `bin/xgca` 378 MB). The configure log
reports `Enabled Kokkos devices: OPENMP;SERIAL;CUDA` and `Activating USE_CUDA`,
and Ginkgo is found (`USE_GINKGO`). Ginkgo prints a warning that it was built
with `g++` and XGC uses `mpicxx`. Both are the same gcc 14.3.1, so ignore it.

### Pitfall: `cannot find -lThrust` when linking

The static Ginkgo's exported target lists `$<LINK_ONLY:Thrust>`, but its
package config does not recreate a `Thrust` target, so the name reaches the
linker as `-lThrust`. Thrust is header-only, so
`find_dependencies_stellarai.cmake` defines an empty `Thrust` interface
target.

### Checks on the build (2026-09-29, `b3fa2c9b1`)

| Check | Result |
|---|---|
| `CMakeCache.txt` | `CMAKE_CXX_COMPILER` = CUDA OpenMPI `mpicxx` (gcc 14.3.1), `USE_GPU=ON`, `Kokkos_DIR`, `Ginkgo_DIR` under `.../<version>/gcc-14.3-cuda-13.2-openmpi-5.0.10` |
| Device flags | `nvcc -arch sm_100 -ccbin mpicxx -extended-lambda -expt-relaxed-constexpr`, host `-O3 -march=sapphirerapids` |
| `ldd` with an empty environment | 0 not found, 0 libraries under `/projects`; libraries only in `/lib64`, `/usr/local/cuda/...` and `/usr/local/openmpi/cuda-13.2/5.0.10/gcc/lib64` (RUNPATH) |
| `xgc --info`, `xgca --info` on vis2 | `USING GPU VERSION`, `Kokkos backend = CUDA`, commit `b3fa2c9b1`, working tree clean |

With no modules loaded, `/usr/local/cuda` points to CUDA 13.4, so `ldd`
resolves the CUDA libraries there. Jobs source the env file, which puts
`cudatoolkit/13.2` on `LD_LIBRARY_PATH`. Same major version either way.

## Running

- 8 ranks per node, one B200 and 12 OpenMP threads each
  (`OMP_NUM_THREADS=12`, `OMP_PROC_BIND=spread`, `OMP_PLACES=cores`).
- `srun -c 12 --cpu-bind=cores --gpus-per-node=8`, with
  `CUDA_VISIBLE_DEVICES=$SLURM_LOCALID` per rank
  (`quickstart/runlines/stellarai.sh`). Check the GPU-to-socket mapping with
  `nvidia-smi topo -m` on a compute node once they are up; the run line assumes
  GPUs 0-3 sit on socket 0.
- **Inter-node traffic uses only `mlx5_11`** (`UCX_NET_DEVICES=mlx5_11:1` in
  the env file, XGC-Devel `c29e1646d`). Each GPU node has 13 HCAs. On
  2026-09-29, `sai-n01g3` had its GPU-rail NDR HCAs (`mlx5_0/3/9/12`) stuck in
  `INIT`, while the other GPU nodes had them `ACTIVE`. With UCX's default
  device choice, any message to or from that node never completed. The 8-node
  jobs 333 and 334 spun at 100% CPU without output, first in hcoll's
  `MPI_Init` allreduce, then (with hcoll off) in the first `MPI_Gather`
  (`taskmap_write`). A two-rank ping-pong between n01g3 and n01g4 fails with
  the default and with `mlx5_4`, and passes with `mlx5_11` (32.8 GB/s). An
  8-node, 64-rank allreduce also passes with `mlx5_11`. `mlx5_11` (NDR 400G,
  `ib11`) is `ACTIVE` on every GPU node. Check the HCA states with
  `cat /sys/class/infiniband/*/ports/1/state` before lifting the restriction:
  it forgoes the multi-rail bandwidth. The test program is
  `/scratch/gpfs/PTHEORY/seungku/Software/mpitest/pp.c`.
- hcoll is also disabled (`OMPI_MCA_coll_hcoll_enable=0`, `70fc0b0a0`). Its
  hang was the same network problem, but Open MPI's own collectives are the
  simpler default. Both settings are in the GPU env file only; the CPU env
  file is unchanged.
- GPU-aware MPI is off (the XGC default). `ompi_info` on vis2 lists the `cuda`
  MPI extension but only the `null` accelerator component; check on a compute
  node before trying `USE_GPU_AWARE_MPI`.
- xgc-automation: the machine entry `stellarai` describes the CPU partition.
  A GPU run passes the GPU settings per job (`partition: gpu`,
  `gpus_per_node: 8`, `ntasks_per_node: 8`, `cpus_per_task: 12`, the GPU
  `env_script` and `xgc_exe`). The flow_match case `nt193793_stellarai_gpu`
  sets them in its `job.stellarai` section.
- **One GPU per rank is required.** When ranks per node equals
  `gpus_per_node`, the Slurm template adds `--gpus-per-task=1
  --gpu-bind=single:1` to srun. Without it every rank sees all 8 GPUs, and
  d0030sa job 331 died in step 1 with `cudaErrorInvalidValue` from a Kokkos
  zero-memset in `CollisionGrid::picard_loop` (3 of 8 ranks). Job 332 with the
  binding ran past step 1.
- The login-node dryrun gate does not apply: the GPU binary cannot run on
  either login node. Dev runs skip it by default; for production runs, dryrun
  the input with the `stellarai_cpu` build on `stellarai-amd`.

## Open items

- Finish the comparison of d0030sa (job 332) with the CPU run n682sa. First
  timing: about 22 s/step on 1 GPU node (8x B200), against about 16 s/step for
  n682sa on 8 CPU nodes. Look at the per-kernel timers before sizing
  production runs.
- Confirm `/projects` is invisible on the GPU nodes too, and that
  `/usr/local/cuda-13.2` and the CUDA OpenMPI are present there.
- Decide how xgc-automation should address the partition (a second machine key
  or a per-run partition/env/exe switch on `stellarai`); simulation-id code.
- Merge `stellarai-gpu` into XGC-Devel `master`.
