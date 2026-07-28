# SSH 멀티플렉싱 설정 (MFA 머신 대응)

XGC 자동화 툴은 **인증을 대신 수행하지 않는다** (AGENTS.md 절대규칙 3).
MFA가 걸린 머신에서 매 명령마다 재인증하지 않으려면, 사람이 한 번 인증해
열어둔 SSH 세션을 ControlMaster 소켓으로 재사용해야 한다.

## 1. `~/.ssh/config` 에 추가

```sshconfig
# --- 공통 멀티플렉싱 ---
Host stellar flux perlmutter polaris
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
    ServerAliveInterval 60
    ServerAliveCountMax 3

Host stellar
    HostName stellar.princeton.edu
    User <princeton_netid>

Host flux
    HostName flux.pppl.gov
    User <pppl_username>
```

`ControlPath` 디렉터리는 미리 존재해야 한다:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
```

주의: macOS에서 `ControlPath` 가 너무 길면 유닉스 소켓 경로 길이 제한
(약 104자)에 걸린다. 위처럼 `~/.ssh/` 바로 아래에 두면 안전하다.

## 2. 사람이 세션을 연다 (MFA는 여기서 1회)

```bash
ssh stellar        # Duo/비밀번호 인증 → 접속되면 그대로 두거나 exit 해도 master 는 8h 유지
```

백그라운드 마스터만 띄우려면:

```bash
ssh -MNf stellar
```

## 3. 상태 확인

```bash
ssh -O check stellar     # "Master running (pid=NNNN)" 이면 정상
```

`preflight` 툴이 이 명령을 그대로 사용한다. 결과 해석:

| `control_master` 값 | 의미 | 조치 |
|---|---|---|
| `alive: true` | 소켓 살아있음, 재인증 불필요 | 없음 |
| `multiplexing_configured: false` | `~/.ssh/config` 에 ControlMaster 설정 없음 | 위 1단계 수행 |
| `alive: false` (설정은 있음) | 마스터가 없거나 만료됨 | 위 2단계 수행 |

## 4. 세션 종료

```bash
ssh -O exit stellar
```

## 네트워크 전제

- `flux.pppl.gov` 는 내부망(172.16.99.x) — PPPL VPN 또는 온사이트 필요.
- `stellar.princeton.edu` 는 외부에서 도달 가능.
- `perlmutter` / `polaris` 는 현재 접근 불가 상태 (2026-07 기준).
