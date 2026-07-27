"""machines.yaml 로더.

머신 정의는 machines.yaml 단일 소스에서만 읽는다. 상위 로직은 머신 종류를
몰라야 하며, 스케줄러/인증/경로 차이는 이 계층이 흡수한다 (AGENTS.md 참조).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# 프로젝트 루트 = 이 파일(tools/machines.py)의 부모의 부모
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACHINES_FILE = PROJECT_ROOT / "machines.yaml"


@lru_cache(maxsize=1)
def load_machines() -> dict[str, Any]:
    """machines.yaml 전체를 dict로 반환."""
    if not MACHINES_FILE.exists():
        raise FileNotFoundError(f"machines.yaml 을 찾을 수 없음: {MACHINES_FILE}")
    with MACHINES_FILE.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def machine_names() -> list[str]:
    """`common` 을 제외한 실제 머신 이름 목록."""
    return [k for k in load_machines().keys() if k != "common"]


def get_machine(name: str) -> dict[str, Any]:
    """이름으로 머신 정의를 반환. 없으면 KeyError."""
    data = load_machines()
    if name not in data or name == "common":
        raise KeyError(f"알 수 없는 머신: {name!r}. 사용 가능: {machine_names()}")
    return data[name]


def is_placeholder(value: Any) -> bool:
    """`<...>` 형태의 미채움 플레이스홀더 여부."""
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def expand_path(value: str) -> str:
    """`~` 와 환경변수를 로컬 기준으로 확장 (원격 경로는 그대로 둘 수 있음)."""
    return os.path.expandvars(os.path.expanduser(value))
