# XGC 빌드 — stellar (Princeton)

검증 일자: 2026-07-28 / XGC-Devel `master` @ `256c3098`

## 요약

```bash
source /projects/XGC/STELLAR/Software/bin/set_up_xgc.stellar

cd ~/git/XGC-Devel
rm -rf build && mkdir build && cd build
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
ninja -j 16 xgc          # 산출물: build/bin/xgc
```

지켜야 할 두 가지. 어기면 빌드가 깨진다:

1. **환경 스크립트를 반드시 source 한다.** 컴파일러(`CC/CXX/FC`)와
   `XGC_PLATFORM=stellar`, `FFTW3DIR`/`HDF5DIR` 를 여기서 설정한다.
2. **`CMAKE_BUILD_TYPE=Release` 를 쓴다.** 기본값이나 `RelWithDebInfo` 는
   컴파일러 크래시로 실패한다 (아래 참조).

## 환경

`/projects/XGC/STELLAR/Software/bin/set_up_xgc.stellar` 가 하는 일:

| 항목 | 값 |
|---|---|
| 컴파일러 | Intel oneAPI 2024.2 (`icpx`/`ifx`) |
| MPI | OpenMPI 4.1.6 (`mpicc`/`mpicxx`/`mpif90`) |
| CMake | 3.24.2 (`cmake/pppl/3.24.2`) |
| 의존성 | `/projects/XGC/STELLAR/Software/install/` (petsc 3.24.4, adios2, kokkos, cabana, pspline, camtimers, gtest) |
| 플랫폼 | `XGC_PLATFORM=stellar` → `CMake/find_dependencies_stellar.cmake` |

컴파일러를 `cmake -DCMAKE_CXX_COMPILER=...` 로 직접 지정하지 말 것.
`find_dependencies_stellar.cmake` 가 `$ENV{FFTW3DIR}` / `$ENV{HDF5DIR}` 에
의존하므로 source 없이는 의존성 탐색이 실패한다.

## 함정 1 — Cray 랩퍼(`ftn`/`CC`/`cc`)

Perlmutter/Polaris 용 configure 명령을 그대로 쓰면:

```
Compiling the Fortran compiler identification source file ... failed.
Compiler: ftn
The output was: No such file or directory
```

`ftn`/`CC` 는 Cray 프로그래밍 환경 전용이고 stellar 에는 없다.
`cc` 는 `/usr/bin/cc`(시스템 GCC)로 존재하기 때문에 더 위험하다 —
에러 없이 잘못된 컴파일러로 빌드된다.

`CMakeCache.txt` 에 한 번 박히면 환경만 고쳐도 캐시 값이 이기므로,
**빌드 디렉터리를 지우고 다시 configure** 해야 한다.

## 함정 2 — `-O2` 에서 icpx 크래시 (핵심)

`RelWithDebInfo`(= `-O2 -g -DNDEBUG`)로 빌드하면:

```
icpx: error: clang frontend command failed with exit code 139
```

exit 139 = SIGSEGV. **소스 버그가 아니라 컴파일러가 죽는 것이다.**
스택 덤프가 원인을 가리킨다:

```
llvm::vpo::VPlanCostModelWithHeuristics<...>::getLoadStoreCost(...)
llvm::vpo::VPlanPeelingAnalysis::selectBestStaticPeelCount(...)
llvm::vpo::LoopVectorizationPlanner::selectBestPlan()
```

icpx 루프 벡터라이저(VPlan)가 peeling 비용 계산 중 크래시한다.
XGC 의 `FORCE_SIMD`(= `#pragma omp simd`, `XGC_core/cpp/utils/simd.hpp:11`)
루프를 `SIMD_SIZE=32` 로 처리할 때 터지는 것으로 보인다.

특정 파일만의 문제가 아니다. 확인된 것만
`XGC_core/cpp/grid/calc_mag_drift_flux_avg.cpp`,
`XGC_core/cpp/sources/current_drive.cpp` 이며, 하나를 우회하면 다음 파일에서
같은 크래시가 난다.

### 플래그별 실측 (두 파일 모두 동일 결과)

| 플래그 | 결과 |
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

`-g` 는 무관하다. **`-O2` 에서만 크래시하고 `-O3` 는 정상이다.**
직관과 반대지만, `-O3` 는 `-O2` 와 벡터화 휴리스틱·비용 모델 경로가 달라
크래시하는 코드 경로를 타지 않는 것으로 보인다.
AVX-512 나 SLP 벡터화는 무관하며, 범인은 루프 벡터라이저 단독이다.

### 왜 `-fno-vectorize` 가 아니라 Release 인가

둘 다 빌드는 통과하지만 성능이 다르다:

| | `-O2 -fno-vectorize` | **Release (`-O3`)** |
|---|---|---|
| 빌드 | 248/248 OK | 248/248 OK |
| `loop not vectorized` 경고 | 395건 | **89건** |
| 바이너리 | 39.3 MB | 40.1 MB |
| 벡터화 | 전면 비활성 | 정상 동작 |

`-fno-vectorize` 는 SIMD 를 통째로 끈다. XGC particle push 는 벡터화가
성능에 직결되므로 프로덕션에 쓰면 안 된다. Release 는 벡터화를 살린 채
크래시만 피한다.

### 디버그 빌드가 필요하면

`RelWithDebInfo` 는 이 컴파일러에서 사용 불가. 대신:

```bash
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-g ..
```

`-O3 -g` 조합은 위 표에서 통과를 확인했다.

### 컴파일러 업그레이드는 현재 불가

stellar 에는 `intel-oneapi/2024.2` 단일 버전만 있다 (`module avail` 확인).
따라서 Release 가 사실상 유일한 해법이다.

이 크래시는 Intel 에 리포트할 가치가 있다. icpx 가 재현용 파일을 남긴다:
`/tmp/icpx-*/calc_mag_drift_flux_avg-*.cpp` 와 `.sh`.

## 미해결 / 관찰된 경고

configure 및 빌드는 통과하지만 확인해둘 항목:

- `HDF5 Fortran compiler wrapper is unable to compile a minimal HDF5 program`
  — CMake 가 라이브러리 직접 지정으로 폴백해 진행됨.
- `Performing Test FILESYSTEM_LINKS_WITHOUT_LIB - Failed`
  — `std::filesystem` 이 별도 라이브러리 요구. 현재 링크는 성공.
- Ginkgo, FusionIO 미발견 — 선택적 의존성이라 무방.
- Kokkos 는 GPU 지원 없이 설치됨 → `USE_OMP;USE_SERIAL` CPU 백엔드.
  stellar 빌드는 CPU 전용(`XGC_TYPES=cpu`).

## 검증 범위

`ninja -j 16 xgc` 로 248/248 타깃, 크래시 0건, `bin/xgc`(40 MB ELF) 생성까지
확인했다. **실행(런타임) 검증은 아직 하지 않았다.**
