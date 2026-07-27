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


def _ohmic_reachable(timeout: float = 4.0) -> dict[str, Any]:
    """로컬에서 ohmic API 엔드포인트 도달성만 확인 (인증/추론 호출 안 함)."""
    base = "https://ohmic.pppl.gov/v1"
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
        host = cfg.get("host")
        # host 가 플레이스홀더가 아니면 SSH 포트(22) 도달성만 가볍게 확인
        ssh_ok = None
        if host and not m.is_placeholder(host):
            ssh_ok = _tcp_reachable(host, 22)

        if unfilled:
            status = "warn"
        elif ssh_ok is False:
            status = "warn"
        else:
            status = "ok"

        machines_report[name] = {
            "status": status,
            "host": host,
            "scheduler": cfg.get("scheduler", {}).get("type"),
            "auth_method": cfg.get("auth", {}).get("method"),
            "ssh_port_22_reachable": ssh_ok,
            "unfilled_config_fields": unfilled,
        }

    report["machines"] = machines_report
    return report


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
