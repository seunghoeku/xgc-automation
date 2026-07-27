#!/usr/bin/env bash
# create_project.sh — xgc-automation 프로젝트 스캐폴드 생성기
# 사용법:  bash create_project.sh   (또는)   chmod +x create_project.sh && ./create_project.sh
set -euo pipefail

ROOT="xgc-automation"
echo "==> creating $ROOT/ ..."
mkdir -p "$ROOT"/{templates,tools,kb,runs}

# ------------------------------------------------------------------ AGENTS.md
cat > "$ROOT/AGENTS.md" <<'EOF'
# XGC 자동화 프로젝트 — 에이전트 지침 (AGENTS.md)

> 이 파일은 opencode가 프로젝트 시작 시 자동으로 읽는 컨텍스트입니다.
> 여기 담긴 배경·규칙·현재 단계를 따라 작업하세요.

## 프로젝트 목표
자연어로 물리 문제를 기술하면 → 로컬 AI가 XGC(gyrokinetic code, xgc.pppl.gov) 입력값을 제안 → 사람이 diff로 검토·수정 → Perlmutter/Polaris에 잡 제출 → 결과 요약을 로컬에서 분석·시각화.
**단일 사용자용**, 빠른 프로토타입 우선. 팀 확장은 나중.

## 핵심 아키텍처: 로컬 두뇌 + 원격 손발
- **LLM은 로컬에서만 실행** (macOS 우선, Linux 호환). 엔드포인트: `ohmic.pppl.gov` API.
- **NERSC 내부는 외부 LLM 호출 금지** → HPC는 순수 SSH 타깃으로만 다룬다.
- LLM은 **제안·설명·조율만** 담당. 실제 잡 제출/파일 삭제/전송 등 위험·결정적 동작은 **검증된 코드(툴)** 가 수행.

## 절대 규칙 (반드시 지킬 것)
1. **제출 전 사람 승인**: `sbatch`/`qsub` 또는 파일 삭제/덮어쓰기 전에 항상 diff/요약을 보여주고 사용자 승인을 받는다. (human-in-the-loop)
2. **결정적 동작은 툴로**: SSH·스케줄러·파일 전송은 LLM이 직접 셸 명령을 지어내지 말고 정의된 툴(`hpc_runner` 등)을 통한다.
3. **인증은 사람이**: MFA/비밀번호를 대신 입력하지 않는다. 세션 확보는 사용자가, 이후 재사용은 ssh ControlMaster로.
4. **할당량 보호**: 잡 제출 전 노드수·wall-time 추정을 제시한다.
5. **프로비넌스 기록**: 어떤 input이 어떤 output을 냈는지 기록한다.

## HPC 대상
| 머신 | 센터 | 스케줄러 | 인증 | 스크래치 |
|---|---|---|---|---|
| Perlmutter | NERSC | Slurm (`sbatch/squeue/scancel`) | sshproxy → 24h SSH key | `$PSCRATCH` |
| Polaris | ALCF | PBS (`qsub/qstat/qdel`) | ssh 터널/포트포워딩 + MFA | `/eagle/<proj>` |

머신 차이는 `machines.yaml` + `hpc_runner` 툴이 흡수한다. 상위 로직은 머신 종류를 몰라야 한다.

## MCP 툴 계층 (구현 대상)
1. `preflight` — ohmic 도달? / Perlmutter key 24h 유효? / Polaris 터널 alive?
2. `physics_advisor` — 문제기술→파라미터 제안 (KB + 규칙/스케일링 하이브리드, 순수 LLM 수치 금지)
3. `input_builder` — Jinja2로 XGC namelist(`input`)·geqdsk 참조·profile 구성
4. `validator` — dt vs 자이로주파수, 그리드 vs rho_i, 입자수/셀, wall-clock 추정
5. `hpc_runner` — `submit/status/cancel/push/pull` (slurm<->pbs 분기)
6. `data_analyzer` — 원격 축약 산출물 생성→로컬 pull→플롯(heat flux, profile, spectra)
7. `provenance` — input→output 계보(커밋/해시/타임스탬프)

## 데이터 원칙
- XGC 출력은 ADIOS2(.bp), 수 TB급 가능. **전량 다운로드 금지**.
- 원격에서 reduce(요약 산출물) 후 소량만 로컬로 pull. 프로토타입은 post-hoc 요약부터.

## 이식성 (macOS 우선, Linux 호환)
- SSH 실동작은 CLI 파싱 대신 Python(paramiko/asyncssh)으로 → BSD/GNU CLI 차이 회피.
- 경로 하드코딩 금지(`~`, `$PSCRATCH`). 배포는 `uv`/`pipx`.
- ssh 멀티플렉싱: `ControlMaster auto` / `ControlPersist 8h` / `ControlPath ~/.ssh/cm-%r@%h:%p`.

## 빌드 자동화
- 현재 범위 밖. XGC 실행파일은 이미 존재 → `machines.yaml`의 `xgc_exe`에 경로 명시.
- 나중에 `hpc_runner.build(machine)` 훅으로 확장(CMake + 머신별 module).

## 현재 단계 / 다음 할 일 (로드맵)
- [ ] 1. `preflight` 툴 — 접속·인증 상태 체크
- [ ] 2. `machines.yaml` 확정 (실제 host/경로/실행파일 채우기)
- [ ] 3. `hpc_runner` MCP — submit/status/cancel/push/pull (slurm+pbs)
- [ ] 4. Perlmutter hello-world sbatch 왕복 성공
- [ ] 5. `input_builder` + `validator`
- [ ] 6. `physics_advisor` (KB+규칙)
- [ ] 7. `data_analyzer` (원격 요약→로컬 플롯)
- [ ] 8. Polaris 어댑터(pbs/qsub)
- [ ] 9. provenance 강화 / (나중) 팀 공유 UI

## 먼저 확인해야 할 미해결 항목
1. **ohmic.pppl.gov API 규격** — OpenAI-호환? 인증(키/SSO/IP)? 로컬에서 VPN 필요?
2. XGC 버전(XGC1 vs XGCa), 주 관심 물리(코어 난류/엣지·SOL/ELM).
3. physics_advisor KB용 과거 성공 input/geqdsk 보유 여부.
4. Superfacility API(잡 제출) 병행 여부.
5. 출력 데이터 규모·스크래치 정리 정책.
EOF

# -------------------------------------------------------------- machines.yaml
cat > "$ROOT/machines.yaml" <<'EOF'
# machines.yaml — HPC 머신 정의 (단일 소스)
# hpc_runner 툴이 이 파일을 읽어 스케줄러/인증/경로 차이를 흡수한다.
# <...> 로 표시된 값은 실제 환경에 맞게 채워야 함.

perlmutter:
  center: nersc
  host: perlmutter-p1.nersc.gov
  user: <nersc_username>
  auth:
    method: sshproxy            # 하루 1회 sshproxy로 24h 유효 key 발급
    key_path: ~/.ssh/nersc      # sshproxy가 생성하는 key
    key_ttl_hours: 24
    ssh_config_host: perlmutter # ~/.ssh/config 에 ControlMaster 설정된 alias 권장
  scheduler:
    type: slurm
    submit: sbatch
    status: squeue
    cancel: scancel
    submit_tpl: templates/perlmutter.sbatch.j2
  paths:
    scratch: $PSCRATCH
    run_root: $PSCRATCH/xgc-runs
  xgc:
    exe: <path/to/xgc-es>       # 이미 빌드된 실행파일 경로
    modules:                    # 실행 전 module load
      - PrgEnv-gnu
      - cray-hdf5-parallel
      - cudatoolkit
      - adios2
  defaults:
    account: <nersc_project>    # -A / --account
    qos: regular
    constraint: gpu
    nodes: 4
    walltime: "01:00:00"

polaris:
  center: alcf
  host: polaris.alcf.anl.gov
  user: <alcf_username>
  auth:
    method: ssh_tunnel          # 로컬 포트포워딩 + MFA
    local_port: 2222
    ssh_config_host: polaris    # ~/.ssh/config alias 권장
  scheduler:
    type: pbs
    submit: qsub
    status: qstat
    cancel: qdel
    submit_tpl: templates/polaris.pbs.j2
  paths:
    scratch: /eagle/<project>
    run_root: /eagle/<project>/xgc-runs
  xgc:
    exe: <path/to/xgc-es>
    modules:
      - PrgEnv-nvhpc
      - cray-hdf5-parallel
      - adios2
  defaults:
    account: <alcf_project>     # -A
    queue: prod
    nodes: 4
    walltime: "01:00:00"

# 공통 설정
common:
  local:
    os_priority: [macos, linux]
    ssh_multiplexing:
      control_master: auto
      control_persist: "8h"
      control_path: "~/.ssh/cm-%r@%h:%p"
  data:
    pull_only_summaries: true   # 전량 다운로드 금지, 요약만 pull
    output_format: adios2       # .bp
EOF

# --------------------------------------------------------------- opencode.json
cat > "$ROOT/opencode.json" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ohmic": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "PPPL ohmic",
      "options": {
        "baseURL": "https://ohmic.pppl.gov/v1",
        "apiKey": "{env:OHMIC_API_KEY}"
      },
      "models": {
        "opus": { "name": "Claude Opus (via ohmic)" },
        "gpt":  { "name": "ChatGPT (via ohmic)" }
      }
    }
  },
  "model": "ohmic/opus",
  "mcp": {
    "xgc-tools": {
      "type": "local",
      "command": ["uv", "run", "python", "-m", "tools.server"],
      "enabled": true
    }
  },
  "permission": {
    "bash": "ask",
    "edit": "ask"
  }
}
EOF

# ----------------------------------------------------------------- README.md
cat > "$ROOT/README.md" <<'EOF'
# xgc-automation

XGC(gyrokinetic code, xgc.pppl.gov) 시뮬레이션·분석 자동화 워크플로우.
**로컬 두뇌 + 원격 손발**: 로컬(macOS/Linux)에서 LLM(ohmic.pppl.gov)을 돌리고,
Perlmutter/Polaris는 SSH로 잡을 제출·모니터·분석한다.

> 상세 설계·규칙은 `AGENTS.md` 참조. opencode는 시작 시 이 파일을 자동 로드한다.

## 디렉토리 구조

