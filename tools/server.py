"""XGC 자동화 MCP 서버.

opencode `opencode.json` 의 mcp.xgc-tools 가 이 모듈을 실행한다:
    uv run python -m tools.server

현재 구현된 툴:
  - list_machines : machines.yaml 에 정의된 머신 목록
  - preflight     : 접속·인증·설정 상태 점검 (AGENTS.md 로드맵 1번)

나머지 툴(physics_advisor/input_builder/validator/hpc_runner/
data_analyzer/provenance)은 로드맵에 따라 이후 추가한다.
"""

from __future__ import annotations

import os
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

from tools import machines as m

mcp = FastMCP("xgc-tools")


@mcp.tool()
def list_machines() -> dict[str, Any]:
    """machines.yaml 에 정의된 HPC 머신 목록과 요약 정보를 반환한다."""
    out: dict[str, Any] = {}
    for name in m.machine_names():
        cfg = m.get_machine(name)
        out[name] = {
            "center": cfg.get("center"),
            "host": cfg.get("host"),
            "scheduler": cfg.get("scheduler", {}).get("type"),
            "auth": cfg.get("auth", {}).get("method"),
        }
    return {"machines": out}


def _check_placeholders(cfg: dict[str, Any], prefix: str = "") -> list[str]:
    """<...> 로 남아있는 미채움 값들의 경로 목록을 재귀 수집."""
    unfilled: list[str] = []
    for key, value in cfg.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            unfilled.extend(_check_placeholders(value, path))
        elif m.is_placeholder(value):
            unfilled.append(path)
    return unfilled


def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _control_socket_alive(ssh_host: str, timeout: float = 5.0) -> dict[str, Any]:
    """ssh ControlMaster 소켓이 살아있는지 확인 (`ssh -O check`).

    MFA 머신에서는 이것이 핵심 지표다. 소켓이 살아있으면 재인증 없이 명령을
    보낼 수 있고, 죽어있으면 사용자가 직접 인증해 세션을 다시 열어야 한다.
    인증을 대신 수행하지 않는다 (AGENTS.md 절대규칙 3).
    """
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check", ssh_host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"alive": False, "detail": "ssh 실행파일을 찾을 수 없음"}
    except subprocess.TimeoutExpired:
        return {"alive": False, "detail": "ssh -O check 타임아웃"}

    # 살아있으면 "Master running (pid=...)" 를 stderr 로 출력하고 rc=0
    msg = (proc.stderr or proc.stdout or "").strip()
    alive = proc.returncode == 0
    # ControlPath 자체가 설정 안 된 경우와 master 가 죽은 경우를 구분한다
    configured = "no controlpath" not in msg.lower()
    return {
        "alive": alive,
        "multiplexing_configured": configured,
        "detail": msg or ("master 실행 중" if alive else "master 없음"),
    }


def _ohmic_reachable(timeout: float = 4.0) -> dict[str, Any]:
    """로컬에서 ohmic API 엔드포인트 도달성만 확인 (인증/추론 호출 안 함)."""
    base = os.environ.get("OHMIC_BASE_URL", "https://api.ohmic.pppl.gov/v1")
    key_present = bool(os.environ.get("OHMIC_API_KEY"))
    try:
        req = urllib.request.Request(base, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        reachable = True
        detail = "HEAD 200/OK 계열"
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 라도 TCP+TLS 는 도달한 것
        reachable = True
        detail = f"HTTP {exc.code} (도달됨)"
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reachable = False
        detail = f"도달 실패: {exc}"
    return {"endpoint": base, "reachable": reachable, "api_key_present": key_present, "detail": detail}


@mcp.tool()
def preflight(machine: str | None = None) -> dict[str, Any]:
    """접속·인증·설정 상태를 점검한다 (제출 전 사전 점검).

    Args:
        machine: 특정 머신만 점검. 생략 시 전체 머신 + ohmic 을 점검.
    Returns:
        각 항목의 상태(ok/warn/fail)와 사람이 읽을 수 있는 진단.
    """
    report: dict[str, Any] = {"ohmic": _ohmic_reachable()}

    targets = [machine] if machine else m.machine_names()
    machines_report: dict[str, Any] = {}
    for name in targets:
        try:
            cfg = m.get_machine(name)
        except KeyError as exc:
            machines_report[name] = {"status": "fail", "error": str(exc)}
            continue

        unfilled = _check_placeholders(cfg)
        auth = cfg.get("auth", {})
        host = cfg.get("host")

        # host 가 플레이스홀더가 아니면 SSH 포트(22) 도달성만 가볍게 확인
        ssh_ok = None
        if host and not m.is_placeholder(host):
            ssh_ok = _tcp_reachable(host, 22)

        # MFA 머신: ControlMaster 소켓이 살아있어야 재인증 없이 명령 전송 가능
        control = None
        ssh_alias = auth.get("ssh_config_host")
        if ssh_alias and not m.is_placeholder(ssh_alias):
            control = _control_socket_alive(ssh_alias)

        # 제출 가능 여부를 가르는 필수 항목만 blocker 로 분류
        blockers = [f for f in unfilled if f.startswith("scheduler.")]
        actions: list[str] = []
        if ssh_ok is False:
            actions.append(f"{host}:22 도달 불가 — VPN/온사이트 네트워크 확인 필요")
        if control is not None and not control["alive"]:
            if not control.get("multiplexing_configured", True):
                actions.append(
                    f"~/.ssh/config 에 Host {ssh_alias} 의 ControlMaster 설정이 없음 — "
                    f"docs/ssh-setup.md 참고해 추가 (MFA 재인증 회피의 전제조건)"
                )
            else:
                actions.append(
                    f"ssh ControlMaster 미기동 — 사용자가 직접 `ssh {ssh_alias}` 로 "
                    f"MFA 인증 후 세션을 열어두면 이후 재사용됨"
                )
        if blockers:
            actions.append(f"스케줄러 설정 미확정: {', '.join(blockers)} — 확정 전 제출 금지")

        if blockers or ssh_ok is False:
            status = "fail"
        elif unfilled or (control is not None and not control["alive"]):
            status = "warn"
        else:
            status = "ok"

        machines_report[name] = {
            "status": status,
            "host": host,
            "scheduler": cfg.get("scheduler", {}).get("type"),
            "auth_method": auth.get("method"),
            "mfa": bool(auth.get("mfa")),
            "ssh_port_22_reachable": ssh_ok,
            "control_master": control,
            "unfilled_config_fields": unfilled,
            "submit_blockers": blockers,
            "next_actions": actions,
        }

    report["machines"] = machines_report
    return report


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
